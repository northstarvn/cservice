"""Runtime multi-tenant database connection routing.

The backend is historically single-tenant: one ``app.db.engine`` and one
session dependency. This module adds an *opt-in* routing layer on top so a
deployment can carve database work per tenant at runtime without changing any
of the existing request paths:

- ``TenantRouter`` lazily creates per-tenant async engines from a DSN template
  (``TENANT_DSN_TEMPLATE`` with a ``{tenant}`` placeholder) or from an
  explicit declaration map (``CSERVICE_TENANTS=a=dsn,b=dsn``).
- Unknown tenants and deployments without a template fall back to the shared
  default engine (single-tenant mode) — behavior is identical to today unless
  someone opts in.
- Registration is runtime: ``register``/``deregister`` can be called while the
  app is serving; connections are pooled per tenant and disposed on shutdown.
- ``resolve_tenant`` answers *should this request run as that tenant* with a
  reason code, after validating the id, the allowlist, and the tenant's
  lifecycle status.
- Read/write splitting (``role="read"``) routes to a replica DSN when one is
  configured, and fails back to the writer.
- Per-tenant health tracking fails an unhealthy tenant over to the shared
  engine instead of taking the request down.
- ``redact_dsn`` keeps credentials out of the introspection catalog.
- ``build_tenant_residency_policy`` — the stage-2 catalog: residency regions,
  lifecycle transitions, and the isolation audit (see below).

Expansion notes (stage 2 — tenancy governance):

Routing correctly is not the same as running a multi-tenant program safely.
Three things a real deployment asks for are missing from the routing answer, and
all three are data rather than code:

- **Data residency.** ``TENANT_RESIDENCY`` binds a tenant (or a class of tenant)
  to a region and forbids routing its data anywhere else. A tenant whose data is
  pinned to ``eu`` must not be served by an engine in ``us``, whatever the DSN
  template says. ``evaluate_residency`` answers that without an engine.
- **Lifecycle transitions.** ``TENANT_LIFECYCLE`` names the legal moves between
  ``active``/``suspended``/``quarantined`` and which of them need a reason.
  ``set_status`` was previously a bare write; ``transition_tenant`` refuses an
  illegal move and reports the reason.
- **Isolation audit.** ``audit_tenant_isolation`` states, per tenant, which
  other tenants' data it can currently reach — through the shared default engine
  — which is the one isolation property routing can silently give away.
  ``build_tenant_residency_policy`` is the catalog for all three.

Nothing here changes ``app.db`` or the existing ``get_db`` dependency.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db import _int_env, engine as default_engine

logger = logging.getLogger(__name__)

DEFAULT_TENANT = os.getenv("CSERVICE_DEFAULT_TENANT", "default")

# Multitenancy is opt-in: with an empty template and no declarations the router
# behaves exactly like the single-tenant backend (everything -> default engine).
TENANT_DSN_TEMPLATE = os.getenv("TENANT_DSN_TEMPLATE", "")
TENANT_READ_DSN_TEMPLATE = os.getenv("TENANT_READ_DSN_TEMPLATE", "")
TENANT_POOL_SIZE = _int_env("TENANT_POOL_SIZE", 3)
TENANT_MAX_OVERFLOW = _int_env("TENANT_MAX_OVERFLOW", 5)
TENANT_POOL_TIMEOUT = _int_env("TENANT_POOL_TIMEOUT", 30)
TENANT_ECHO = os.getenv("TENANT_ECHO", "false").strip().lower() in {"1", "true", "yes", "on"}

# Tenant lifecycle states. ``suspended`` routes to the shared engine on purpose:
# the tenant still reads, but never touches its own dedicated database.
TENANT_STATUSES = ("active", "suspended", "quarantined")

# Every reason ``resolve_tenant`` can return.
TENANT_RESOLUTION_REASONS = (
    "ok",
    "invalid_tenant_id",
    "not_allowed",
    "suspended",
    "quarantined",
)

TENANT_ROLES = ("write", "read")

# Router policy, all overridable per instance and per environment.
TENANT_ROUTER_POLICY: dict[str, Any] = {
    "id_pattern": r"^[a-z0-9][a-z0-9._-]{0,62}$",
    "allowlist": (),
    "auto_register": True,
    "failover_enabled": True,
    "health_failure_threshold": 3,
    "health_window_seconds": 60,
    "max_engines": 32,
    "track_usage": True,
    "usage_window": 1000,
}

# --- data residency (expansion) ------------------------------------------------
#
# Routing a request to the right *database* says nothing about which
# *jurisdiction* its data lands in. A tenant whose records must stay in the EU
# cannot be served by an engine in the US, so the region is part of a tenant's
# identity and is evaluated before an engine is handed out.
#
# Config table: region code -> {label, allowed_tenants, blocked_regions,
# write_home_only, enforce}. ``allowed_tenants: ()`` means "no tenant is pinned
# here", and ``enforce: False`` makes the whole region advisory.
TENANT_RESIDENCY: dict[str, dict[str, Any]] = {
    "eu": {
        "label": "European Union",
        "allowed_tenants": (),
        "blocked_regions": ("us",),
        "write_home_only": True,
        "enforce": True,
    },
    "us": {
        "label": "United States",
        "allowed_tenants": (),
        "blocked_regions": ("eu",),
        "write_home_only": True,
        "enforce": True,
    },
    "global": {
        "label": "no residency constraint",
        "allowed_tenants": (),
        "blocked_regions": (),
        "write_home_only": False,
        "enforce": False,
    },
}
# Where a tenant's data lives when no entry pins it.
DEFAULT_RESIDENCY_REGION = "global"
# Regions a request may be *served from* even when a tenant is pinned. A read
# replica in the tenant's own region is always acceptable; anything else is not.
TENANT_RESIDENCY_REASONS = (
    "ok",
    "region_unknown",
    "tenant_not_permitted",
    "region_blocked",
    "enforcement_disabled",
)
# Metadata label that carries a tenant's pinned region.
RESIDENCY_METADATA_FIELD = "residency_region"


def residency_region_for(
    tenant_id: str, metadata: dict[str, Any] | None = None
) -> str:
    """A tenant's pinned region: its metadata label, else the first pin, else default.

    Deliberately a two-source lookup so a region's ``allowed_tenants`` table and
    a tenant's own metadata can both express the same pin without disagreeing
    about which one wins.
    """
    declared = (metadata or {}).get(RESIDENCY_METADATA_FIELD)
    if declared:
        return str(declared)
    for region, config in TENANT_RESIDENCY.items():
        if tenant_id in (config.get("allowed_tenants") or ()):
            return region
    return DEFAULT_RESIDENCY_REGION


def evaluate_residency(
    tenant_id: str,
    target_region: str | None,
    *,
    metadata: dict[str, Any] | None = None,
    role: str = "write",
) -> dict[str, Any]:
    """May a tenant's work run in ``target_region``?

    Pure — no engine, no DSN — so residency is checkable in a plan, a
    dry-run, or a review as easily as in a request. A ``None`` target means
    "wherever the routing would put it", which is only acceptable when the
    tenant is unpinned.
    """
    home = residency_region_for(tenant_id, metadata)
    home_config = TENANT_RESIDENCY.get(home) or {}
    verdict: dict[str, Any] = {
        "tenant_id": tenant_id,
        "home_region": home,
        "target_region": target_region,
        "role": role,
        "allowed": True,
        "reason": "ok",
    }
    if home_config.get("enforce", True) and target_region is not None:
        permitted = home_config.get("allowed_tenants") or ()
        if permitted and tenant_id not in permitted:
            verdict.update(allowed=False, reason="tenant_not_permitted")
            return verdict
    if target_region is None:
        # A pinned tenant cannot fall through to an unknown location.
        if home_config.get("enforce", True) and home != DEFAULT_RESIDENCY_REGION:
            verdict.update(allowed=False, reason="region_unknown")
        elif not home_config.get("enforce", True):
            verdict["reason"] = "enforcement_disabled"
        return verdict
    target = TENANT_RESIDENCY.get(target_region)
    if target is None:
        verdict.update(allowed=False, reason="region_unknown")
        return verdict
    if not target.get("enforce", True):
        verdict["reason"] = "enforcement_disabled"
        return verdict
    if target_region in (home_config.get("blocked_regions") or ()):
        verdict.update(allowed=False, reason="region_blocked")
        return verdict
    if home_config.get("write_home_only", False) and role == "write" and target_region != home:
        verdict.update(allowed=False, reason="region_blocked")
    return verdict


# --- lifecycle transitions (expansion) ------------------------------------------
#
# ``set_status`` writes a status; whether that is a *permitted* move is a policy
# question. Config table: from-status -> {to-statuses}, plus whether a reason
# is required. Closing an open transition is what stops a suspended tenant being
# quietly reactivated.
TENANT_LIFECYCLE: dict[str, dict[str, Any]] = {
    "active": {
        "to": ("suspended", "quarantined"),
        "requires_reason": True,
        "note": "an active tenant may be suspended or quarantined",
    },
    "suspended": {
        "to": ("active", "quarantined"),
        "requires_reason": True,
        "note": "suspension is lifted explicitly, never implicitly",
    },
    "quarantined": {
        "to": ("active",),
        "requires_reason": True,
        "note": "quarantine is only left via an active transition; a suspended "
                "quarantined tenant must be activated deliberately",
    },
}
# Why a transition was refused, so a caller branches on a closed set.
TENANT_TRANSITION_REASONS = ("ok", "illegal_transition", "reason_required", "unknown_status")


def _env_allowlist() -> tuple[str, ...]:
    raw = os.getenv("CSERVICE_TENANT_ALLOWLIST", "") or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def build_router_policy(overrides: dict | None = None) -> dict[str, Any]:
    """Router policy with the environment allowlist folded in."""
    policy = {**TENANT_ROUTER_POLICY, **dict(overrides or {})}
    if not policy.get("allowlist"):
        policy["allowlist"] = _env_allowlist()
    return policy


def compile_tenant_pattern(pattern: str | None = None) -> re.Pattern[str]:
    """Tenant-id validator; the source of the pattern is always introspectable."""
    raw = pattern or os.getenv("CSERVICE_TENANT_ID_PATTERN") or TENANT_ROUTER_POLICY["id_pattern"]
    return re.compile(raw)


def is_valid_tenant_id(tenant_id: str, *, pattern: re.Pattern[str] | None = None) -> bool:
    """Tenant ids are lowercase, bounded, and never path-like."""
    candidate = (tenant_id or "").strip()
    if not candidate or len(candidate) > 64:
        return False
    return bool((pattern or compile_tenant_pattern()).match(candidate))


def normalize_tenant_id(tenant_id: str | None) -> str:
    return (tenant_id or "").strip() or DEFAULT_TENANT


def redact_dsn(dsn: str | None) -> str | None:
    """Strip credentials from a DSN so a catalog can expose it safely."""
    if not dsn:
        return dsn
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "***redacted***"
    if not parts.scheme or not parts.netloc:
        return "***redacted***"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    user = parts.username or ""
    netloc = f"{user}:***@{host}" if user else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def parse_declared_tenants() -> dict[str, str]:
    """Declared tenant -> DSN map from ``CSERVICE_TENANTS`` and the template.

    ``CSERVICE_TENANTS`` format: ``acme=postgresql+asyncpg://...:5432/cservice_acme,beta=...``
    """
    declared: dict[str, str] = {}
    raw = os.getenv("CSERVICE_TENANTS", "") or ""
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        tenant_id, dsn = part.split("=", 1)
        tenant_id, dsn = tenant_id.strip(), dsn.strip()
        if tenant_id and dsn:
            declared[tenant_id] = dsn
    if TENANT_DSN_TEMPLATE and DEFAULT_TENANT not in declared and "{tenant}" in TENANT_DSN_TEMPLATE:
        declared.setdefault(
            DEFAULT_TENANT,
            TENANT_DSN_TEMPLATE.replace("{tenant}", DEFAULT_TENANT),
        )
    return declared


def _resolve_dsn(tenant_id: str, dsn: str | None, role: str = "write") -> str | None:
    """DSN for a tenant, preferring an explicit value then the role's template."""
    if dsn:
        return dsn
    declared = parse_declared_tenants()
    template = TENANT_READ_DSN_TEMPLATE if role == "read" else TENANT_DSN_TEMPLATE
    if template and "{tenant}" in template:
        candidate = template.replace("{tenant}", tenant_id)
        if candidate:
            return candidate
    if role == "read":
        # A read replica is optional: fall back to the write DSN rather than
        # silently routing reads to the shared default engine.
        return declared.get(tenant_id) or None
    return declared.get(tenant_id) or None


@dataclass
class TenantResolution:
    """Why a request may (or may not) run as a given tenant."""

    tenant_id: str
    source: str
    reason: str = "ok"
    status: str = "active"
    mode: str = "shared_default"
    role: str = "write"

    @property
    def allowed(self) -> bool:
        return self.reason == "ok"

    def to_payload(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "source": self.source,
            "reason": self.reason,
            "status": self.status,
            "mode": self.mode,
            "role": self.role,
            "allowed": self.allowed,
        }


class TenantRouter:
    """Routes database work to per-tenant async engines at runtime.

    Engines are created lazily on first use, pooled per tenant, and disposed on
    shutdown. A tenant without a dedicated DSN resolves to the shared default
    engine, so routing never isolates a tenant from data it legitimately shares.
    """

    def __init__(self, engine=default_engine, policy: dict[str, Any] | None = None):
        self._default_engine = engine
        self._engines: dict[str, Any] = {}
        self._specs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._policy = build_router_policy(policy)
        self._pattern = compile_tenant_pattern(self._policy.get("id_pattern"))
        self._statuses: dict[str, str] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._health: dict[str, dict[str, Any]] = {}
        self._usage: dict[str, deque[str]] = {}
        self._pool_overrides: dict[str, dict[str, Any]] = {}
        self._recent: deque[str] = deque(maxlen=max(1, int(self._policy.get("max_engines", 32))))

    # --- policy -------------------------------------------------------------

    @property
    def policy(self) -> dict[str, Any]:
        return dict(self._policy)

    def allowlist(self) -> tuple[str, ...]:
        return tuple(self._policy.get("allowlist") or ())

    def is_allowed(self, tenant_id: str) -> bool:
        allowed = self.allowlist()
        return not allowed or tenant_id in allowed

    def status_for(self, tenant_id: str) -> str:
        return self._statuses.get(tenant_id, "active")

    def set_status(self, tenant_id: str, status: str) -> dict[str, Any]:
        """Move a tenant between ``active`` / ``suspended`` / ``quarantined``."""
        tenant_id = normalize_tenant_id(tenant_id)
        if status not in TENANT_STATUSES:
            raise ValueError(f"status must be one of {', '.join(TENANT_STATUSES)}")
        previous = self.status_for(tenant_id)
        self._statuses[tenant_id] = status
        return {
            "tenant_id": tenant_id,
            "status": status,
            "previous_status": previous,
            "changed": previous != status,
            "at": datetime.now(timezone.utc).isoformat(),
        }

    def transition_tenant(
        self, tenant_id: str, status: str, *, reason: str = "", force: bool = False
    ) -> dict[str, Any]:
        """Move a tenant's status only if :data:`TENANT_LIFECYCLE` allows it.

        The policy-checked counterpart to :meth:`set_status`, which stays a bare
        write so every existing call site keeps its exact behaviour. An illegal
        move is *refused and reported* rather than raised, so an operator sees
        both the verdict and the states that were actually available.
        """
        tenant_id = normalize_tenant_id(tenant_id)
        previous = self.status_for(tenant_id)
        if status not in TENANT_STATUSES:
            return {
                "tenant_id": tenant_id,
                "from": previous,
                "to": status,
                "applied": False,
                "reason": "unknown_status",
                "allowed": [],
            }
        rule = TENANT_LIFECYCLE.get(previous) or {}
        allowed = list(rule.get("to") or ())
        if previous == status:
            return {
                "tenant_id": tenant_id,
                "from": previous,
                "to": status,
                "applied": False,
                "reason": "ok",
                "allowed": allowed,
                "noop": True,
            }
        verdict = "ok"
        if not force and status not in allowed:
            verdict = "illegal_transition"
        elif not force and rule.get("requires_reason", True) and not str(reason).strip():
            verdict = "reason_required"
        if verdict != "ok":
            return {
                "tenant_id": tenant_id,
                "from": previous,
                "to": status,
                "applied": False,
                "reason": verdict,
                "allowed": allowed,
                "forced": bool(force),
            }
        outcome = self.set_status(tenant_id, status)
        return {
            **outcome,
            "from": previous,
            "to": status,
            "applied": True,
            "reason": "ok",
            "allowed": allowed,
            "justification": reason,
            "forced": bool(force),
        }

    def set_metadata(self, tenant_id: str, **labels: Any) -> dict[str, Any]:
        """Attach deployment metadata (region, plan, contact) to a tenant."""
        tenant_id = normalize_tenant_id(tenant_id)
        current = self._metadata.setdefault(tenant_id, {})
        current.update({k: v for k, v in labels.items() if v is not None})
        return {"tenant_id": tenant_id, "metadata": dict(current)}

    def metadata_for(self, tenant_id: str) -> dict[str, Any]:
        return dict(self._metadata.get(normalize_tenant_id(tenant_id), {}))

    def set_pool_overrides(self, tenant_id: str, **overrides: Any) -> dict[str, Any]:
        """Per-tenant pool sizing, e.g. a noisy tenant gets a bigger pool."""
        tenant_id = normalize_tenant_id(tenant_id)
        allowed = {"pool_size", "max_overflow", "pool_timeout", "echo"}
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(
                f"unsupported pool override(s): {', '.join(sorted(unknown))}"
            )
        current = self._pool_overrides.setdefault(tenant_id, {})
        current.update({k: v for k, v in overrides.items() if v is not None})
        return {"tenant_id": tenant_id, "pool": dict(current)}

    def effective_pool(self, tenant_id: str) -> dict[str, Any]:
        """Pool settings a tenant's engine is (or would be) created with."""
        overrides = self._pool_overrides.get(normalize_tenant_id(tenant_id), {})
        return {
            "size": overrides.get("pool_size", TENANT_POOL_SIZE),
            "max_overflow": overrides.get("max_overflow", TENANT_MAX_OVERFLOW),
            "timeout": overrides.get("pool_timeout", TENANT_POOL_TIMEOUT),
            "echo": overrides.get("echo", TENANT_ECHO),
        }

    # --- health & usage -----------------------------------------------------

    def record_outcome(self, tenant_id: str, ok: bool) -> dict[str, Any]:
        """Record a success/failure for failover and observability."""
        tenant_id = normalize_tenant_id(tenant_id)
        state = self._health.setdefault(
            tenant_id,
            {"ok": 0, "failed": 0, "last_ok_at": None, "last_failure_at": None},
        )
        now = datetime.now(timezone.utc).isoformat()
        if ok:
            state["ok"] += 1
            state["failed"] = 0
            state["last_ok_at"] = now
        else:
            state["failed"] += 1
            state["last_failure_at"] = now
        return {"tenant_id": tenant_id, **state}

    def is_healthy(self, tenant_id: str) -> bool:
        if not self._policy.get("failover_enabled", True):
            return True
        state = self._health.get(normalize_tenant_id(tenant_id))
        if not state:
            return True
        threshold = int(self._policy.get("health_failure_threshold", 0) or 0)
        return int(state.get("failed", 0)) < threshold

    def unhealthy_tenants(self) -> list[str]:
        return sorted(t for t in self._specs if not self.is_healthy(t))

    def record_request(self, tenant_id: str) -> None:
        if not self._policy.get("track_usage", True):
            return
        window = self._usage.setdefault(
            normalize_tenant_id(tenant_id), deque(maxlen=int(self._policy.get("usage_window", 1000)))
        )
        window.append(datetime.now(timezone.utc).isoformat())

    def usage(self) -> dict[str, dict[str, Any]]:
        now = datetime.now(timezone.utc)
        return {
            tenant_id: {
                "requests": len(window),
                "last_seen_at": window[-1] if window else None,
                "idle_seconds": round((now - datetime.fromisoformat(window[-1])).total_seconds(), 3)
                if window
                else None,
            }
            for tenant_id, window in sorted(self._usage.items())
        }

    # --- resolution ---------------------------------------------------------

    def resolve_tenant(
        self,
        tenant_id: str | None = None,
        *,
        source: str = "explicit",
        role: str = "write",
    ) -> TenantResolution:
        """Decide whether a request may run as ``tenant_id``, and why.

        Ordering matters: a malformed id is rejected before the allowlist is
        consulted, and lifecycle status is checked last so an operator can see
        *both* that a tenant is unknown and that it is quarantined.
        """
        if role not in TENANT_ROLES:
            raise ValueError(f"role must be one of {', '.join(TENANT_ROLES)}")
        tenant_id = normalize_tenant_id(tenant_id)
        if not is_valid_tenant_id(tenant_id, pattern=self._pattern):
            return TenantResolution(tenant_id, source, "invalid_tenant_id", role=role)
        if not self.is_allowed(tenant_id):
            return TenantResolution(tenant_id, source, "not_allowed", role=role)
        status = self.status_for(tenant_id)
        spec = self._specs.get(tenant_id) or {}
        if status == "quarantined":
            return TenantResolution(
                tenant_id, source, "quarantined", status, spec.get("mode", "shared_default"), role
            )
        if status == "suspended":
            return TenantResolution(
                tenant_id, source, "suspended", status, "shared_default", role
            )
        return TenantResolution(
            tenant_id, source, "ok", status, spec.get("mode", "shared_default"), role
        )

    async def register(
        self,
        tenant_id: str,
        dsn: str | None = None,
        *,
        pool_size: int | None = None,
        max_overflow: int | None = None,
        pool_timeout: int | None = None,
        echo: bool | None = None,
        role: str = "write",
    ) -> Any:
        """Register (or return) the engine for ``tenant_id``.

        ``dsn`` may be omitted to use the template/declaration map; when no DSN
        is resolvable the tenant routes to the shared default engine.
        """
        tenant_id = (tenant_id or DEFAULT_TENANT).strip()
        if role not in TENANT_ROLES:
            raise ValueError(f"role must be one of {', '.join(TENANT_ROLES)}")
        key = tenant_id if role == "write" else f"{tenant_id}#read"
        if key in self._specs:
            return self._engines[key]
        resolved = _resolve_dsn(tenant_id, dsn, role)
        async with self._lock:
            if key in self._specs:
                return self._engines[key]
            if not resolved:
                self._specs[key] = {
                    "tenant_id": tenant_id,
                    "dsn": None,
                    "mode": "shared_default",
                    "role": role,
                }
                self._engines[key] = self._default_engine
                return self._default_engine
            settings = self.effective_pool(tenant_id)
            engine = create_async_engine(
                resolved,
                echo=settings["echo"] if echo is None else echo,
                pool_size=pool_size or settings["size"],
                max_overflow=max_overflow
                if max_overflow is not None
                else settings["max_overflow"],
                pool_timeout=pool_timeout or settings["timeout"],
            )
            self._engines[key] = engine
            self._specs[key] = {
                "tenant_id": tenant_id,
                "dsn": resolved,
                "mode": "dedicated_engine",
                "role": role,
            }
            self._recent.append(key)
            logger.info(
                "Tenant engine registered: %s (dedicated, role=%s)", tenant_id, role
            )
            await self._evict_if_needed()
            return engine

    async def _evict_if_needed(self) -> list[str]:
        """Dispose the least-recently-used dedicated engines over the cap."""
        cap = int(self._policy.get("max_engines", 0) or 0)
        evicted: list[str] = []
        while cap and len(self._engines) > cap:
            candidates = [k for k in self._recent if k in self._engines]
            if not candidates:
                break
            victim = candidates[0]
            engine = self._engines.pop(victim, None)
            self._specs.pop(victim, None)
            self._recent.remove(victim)
            if engine is not None and engine is not self._default_engine:
                await engine.dispose()
            evicted.append(victim)
        return evicted

    async def deregister(self, tenant_id: str) -> bool:
        """Drop a tenant's engine and dispose it (never touches the default)."""
        keys = [
            key
            for key in self._engines
            if key == tenant_id or key == f"{tenant_id}#read"
        ]
        if not keys:
            return False
        async with self._lock:
            for key in keys:
                engine = self._engines.pop(key, None)
                self._specs.pop(key, None)
                if key in self._recent:
                    self._recent.remove(key)
                if engine is not None and engine is not self._default_engine:
                    await engine.dispose()
            logger.info("Tenant engine deregistered: %s", tenant_id)
        return True

    async def engine_for(self, tenant_id: str | None = None, *, role: str = "write") -> Any:
        """Resolve the engine for a tenant, registering it lazily if needed.

        Honors the policy: a suspended/quarantined/not-allowed tenant and an
        unhealthy dedicated engine both fall back to the shared engine.
        """
        tenant_id = (tenant_id or DEFAULT_TENANT).strip()
        resolution = self.resolve_tenant(tenant_id, role=role)
        key = tenant_id if role == "write" else f"{tenant_id}#read"
        if key not in self._specs and self._policy.get("auto_register", True):
            await self.register(tenant_id, role=role)
        engine = self._engines.get(key, self._default_engine)
        if resolution.reason != "ok":
            return self._default_engine
        if not self.is_healthy(tenant_id):
            return self._default_engine
        self._recent.append(key)
        self.record_request(tenant_id)
        return engine

    async def session_factory_for(self, tenant_id: str | None = None, *, role: str = "write"):
        """A fresh ``sessionmaker`` bound to the tenant's engine."""
        engine = await self.engine_for(tenant_id, role=role)
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Current routing table (tenant -> mode/dsn), for introspection.

        Keyed by tenant id, with the read role suffixed so a tenant that has
        both a writer and a replica is visible as two entries.
        """
        rows: dict[str, dict[str, Any]] = {}
        for spec in sorted(
            self._specs.values(),
            key=lambda s: (s["tenant_id"], s.get("role", "write")),
        ):
            tenant_id = spec["tenant_id"]
            role = spec.get("role", "write")
            key = tenant_id if role == "write" else f"{tenant_id}#read"
            health = self._health.get(tenant_id) or {}
            rows[key] = {
                "mode": spec["mode"],
                "dsn": spec.get("dsn"),
                "role": role,
                "status": self.status_for(tenant_id),
                "healthy": self.is_healthy(tenant_id),
                "dsn_redacted": redact_dsn(spec.get("dsn")),
                "metadata": self.metadata_for(tenant_id),
                "pool": self.effective_pool(tenant_id),
                "health": {
                    "ok": int(health.get("ok", 0)),
                    "failed": int(health.get("failed", 0)),
                    "last_ok_at": health.get("last_ok_at"),
                    "last_failure_at": health.get("last_failure_at"),
                },
                "requests": len(self._usage.get(tenant_id, ())),
            }
        return rows

    def plan_provisioning(self, tenant_ids: list[str] | None = None) -> dict[str, Any]:
        """What would be created for a set of tenants, without creating it."""
        candidates = list(tenant_ids or sorted(parse_declared_tenants()))
        rows = []
        for tenant_id in candidates:
            valid = is_valid_tenant_id(tenant_id, pattern=self._pattern)
            allowed = self.is_allowed(tenant_id)
            write_dsn = _resolve_dsn(tenant_id, None, "write")
            read_dsn = _resolve_dsn(tenant_id, None, "read")
            if not valid:
                action, reason = "reject", "invalid_tenant_id"
            elif not allowed:
                action, reason = "reject", "not_allowed"
            elif write_dsn:
                action, reason = "create_engine", "dedicated"
            else:
                action, reason = "route_shared", "no_dsn"
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "action": action,
                    "reason": reason,
                    "mode": "dedicated_engine" if write_dsn else "shared_default",
                    "read_replica": bool(read_dsn and read_dsn != write_dsn),
                    "dsn": redact_dsn(write_dsn),
                    "status": self.status_for(tenant_id),
                    "registered": tenant_id in self._specs,
                }
            )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "candidates": len(rows),
            "rows": rows,
            "pool_defaults": {
                "size": TENANT_POOL_SIZE,
                "max_overflow": TENANT_MAX_OVERFLOW,
                "timeout": TENANT_POOL_TIMEOUT,
                "echo": TENANT_ECHO,
            },
        }

    # --- residency & isolation --------------------------------------------

    def residency_for(self, tenant_id: str) -> str:
        """A tenant's pinned region, from its metadata or the region table."""
        return residency_region_for(
            normalize_tenant_id(tenant_id), self.metadata_for(tenant_id)
        )

    def evaluate_residency(
        self, tenant_id: str, target_region: str | None, *, role: str = "write"
    ) -> dict[str, Any]:
        """Instance-scoped residency verdict, using the router's own metadata."""
        return evaluate_residency(
            normalize_tenant_id(tenant_id),
            target_region,
            metadata=self.metadata_for(tenant_id),
            role=role,
        )

    def isolation_audit(self) -> dict[str, Any]:
        """Which tenants can currently reach which other tenants' data.

        The one isolation property routing can give away silently: a tenant with
        no dedicated DSN routes to the *shared* default engine, so it is not
        isolated from any other shared tenant. Stating that per tenant, rather
        than inferring it from the snapshot, is what makes it reviewable.
        """
        rows: list[dict[str, Any]] = []
        for tenant_id in sorted(set(self._specs) | set(self._statuses) | set(self._metadata)):
            clean = tenant_id.split("#", 1)[0]
            modes = [
                spec["mode"]
                for spec in self._specs.values()
                if spec["tenant_id"] == clean
            ]
            shared = any(mode == "shared_default" for mode in modes)
            dedicated = any(mode == "dedicated_engine" for mode in modes)
            rows.append(
                {
                    "tenant_id": clean,
                    "mode": "dedicated_engine" if dedicated else "shared_default",
                    "isolated": dedicated,
                    "shares_default_engine": shared,
                    "residency_region": self.residency_for(clean),
                    "status": self.status_for(clean),
                    "registered": bool(modes),
                }
            )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tenants": rows,
            "tenant_count": len(rows),
            "isolated": sum(1 for row in rows if row["isolated"]),
            "shared": sum(1 for row in rows if row["shares_default_engine"]),
            "unisolated_tenants": [row["tenant_id"] for row in rows if not row["isolated"]],
        }

    async def dispose_all(self) -> None:
        """Dispose every created tenant engine (shutdown hook)."""
        async with self._lock:
            for tenant_id, engine in list(self._engines.items()):
                if engine is not self._default_engine:
                    await engine.dispose()
            self._engines.clear()
            self._specs.clear()
            self._recent.clear()


# Process-wide router; endpoints and workers share it.
registry = TenantRouter()


async def get_tenant_session(
    tenant_id: str | None = None, *, role: str = "write"
) -> AsyncIterator[AsyncSession]:
    """Async session scoped to a tenant (prefer this over raw engines)."""
    factory = await registry.session_factory_for(tenant_id, role=role)
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_tenant_db(tenant_id: str, *, role: str = "write") -> AsyncIterator[AsyncSession]:
    """FastAPI dependency form of :func:`get_tenant_session`."""
    async for session in get_tenant_session(tenant_id, role=role):
        yield session


def get_request_tenant_id(request: Request) -> str:
    """Resolve tenant scope for a request: header, query, then default.

    Order: ``X-Tenant-ID`` header -> ``?tenant_id=`` query param ->
    ``CSERVICE_DEFAULT_TENANT``.
    """
    header = (request.headers.get("x-tenant-id") or "").strip()
    if header:
        return header
    query = (request.query_params.get("tenant_id") or "").strip()
    if query:
        return query
    return DEFAULT_TENANT


def resolve_request_tenant(
    request: Request,
    router: TenantRouter | None = None,
    *,
    role: str = "write",
) -> TenantResolution:
    """Like :func:`get_request_tenant_id`, but policy-checked and labelled.

    ``get_request_tenant_id`` stays the raw lookup so existing call sites keep
    their exact precedence; this wrapper adds the source it resolved from and the
    allowlist/status verdict.
    """
    header = (request.headers.get("x-tenant-id") or "").strip()
    if header:
        source = "X-Tenant-ID header"
        tenant_id = header
    else:
        query = (request.query_params.get("tenant_id") or "").strip()
        if query:
            source = "tenant_id query param"
            tenant_id = query
        else:
            source = "default tenant"
            tenant_id = DEFAULT_TENANT
    return (router or registry).resolve_tenant(tenant_id, source=source, role=role)


async def get_request_tenant_session(
    request: Request, *, role: str = "write"
) -> AsyncIterator[AsyncSession]:
    """Tenant session resolved from the request, with policy applied."""
    resolution = resolve_request_tenant(request, role=role)
    async for session in get_tenant_session(resolution.tenant_id, role=role):
        yield session


def build_tenant_router_catalog() -> dict[str, object]:
    """Introspectable catalog for metadata endpoints.

    The top-level key set is a pinned contract, so new capability is reported
    *inside* the existing keys (``pool``, ``registered``) rather than as new
    siblings. Routing policy that does not fit those shapes lives in
    :func:`build_tenant_routing_policy` instead.
    """
    declared = parse_declared_tenants()
    routed = bool(TENANT_DSN_TEMPLATE) or bool(declared)
    policy = registry.policy
    return {
        "default_tenant": DEFAULT_TENANT,
        "mode": "routed" if routed else "shared_default",
        "dsn_template_configured": bool(TENANT_DSN_TEMPLATE),
        "declared_tenants": {
            tenant_id: redact_dsn(dsn) for tenant_id, dsn in sorted(declared.items())
        },
        "registered": registry.snapshot(),
        "pool": {
            "size": TENANT_POOL_SIZE,
            "max_overflow": TENANT_MAX_OVERFLOW,
            "timeout": TENANT_POOL_TIMEOUT,
            "echo": TENANT_ECHO,
            "roles": list(TENANT_ROLES),
            "max_engines": int(policy.get("max_engines", 0)),
            "read_replica_template": bool(TENANT_READ_DSN_TEMPLATE),
            "read_replica_dsn": redact_dsn(TENANT_READ_DSN_TEMPLATE) or None,
            "overrides": {
                tenant_id: registry.effective_pool(tenant_id)
                for tenant_id in sorted(registry._pool_overrides)
            },
            "usage": registry.usage(),
        },
        "lookup_order": [
            "X-Tenant-ID header",
            "tenant_id query param",
            "default tenant",
        ],
    }


def build_tenant_routing_policy(router: TenantRouter | None = None) -> dict[str, object]:
    """The routing policy that governs :func:`build_tenant_router_catalog`.

    Kept separate because the catalog's key set is pinned: this is where the
    id pattern, allowlist, lifecycle states, resolution reason codes, and the
    provisioning plan are introspected.
    """
    router = router or registry
    policy = router.policy
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": policy,
        "default_tenant": DEFAULT_TENANT,
        "id_pattern": policy.get("id_pattern"),
        "id_pattern_source": os.getenv("CSERVICE_TENANT_ID_PATTERN", "builtin"),
        "allowlist": list(router.allowlist()),
        "statuses": list(TENANT_STATUSES),
        "roles": list(TENANT_ROLES),
        "resolution_reasons": list(TENANT_RESOLUTION_REASONS),
        "sources": ["X-Tenant-ID header", "tenant_id query param", "default tenant"],
        "statuses_by_tenant": dict(sorted(router._statuses.items())),
        "metadata_by_tenant": {
            tenant_id: router.metadata_for(tenant_id)
            for tenant_id in sorted(router._metadata)
        },
        "health": {
            "unhealthy": router.unhealthy_tenants(),
            "failover_enabled": bool(policy.get("failover_enabled", True)),
            "failure_threshold": int(policy.get("health_failure_threshold", 0)),
        },
        "provisioning": router.plan_provisioning(),
        # --- stage 2: residency + lifecycle (expansion) --------------------
        "residency": build_tenant_residency_policy(router),
        "lifecycle": {
            "transitions": {
                status: list(config.get("to") or ())
                for status, config in sorted(TENANT_LIFECYCLE.items())
            },
            "reasons": list(TENANT_TRANSITION_REASONS),
            "statuses_by_tenant": dict(sorted(router._statuses.items())),
        },
        "note": (
            "tenant routing is opt-in; ids are pattern-validated, the allowlist "
            "and lifecycle status are checked before an engine is handed out, and "
            "DSNs are redacted in every introspection surface"
        ),
    }


def build_tenant_residency_policy(router: TenantRouter | None = None) -> dict[str, object]:
    """Data-residency and isolation-audit policy for a tenant fleet.

    Its own catalog because the residency rules answer a different question from
    routing: not "where does this request go" but "may it go there at all". The
    isolation audit is included because the two answer each other — a tenant
    routed to the shared engine has no residency guarantee to enforce.
    """
    router = router or registry
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "regions": {
            code: {
                "label": config.get("label", ""),
                "allowed_tenants": list(config.get("allowed_tenants") or ()),
                "blocked_regions": list(config.get("blocked_regions") or ()),
                "write_home_only": bool(config.get("write_home_only", False)),
                "enforce": bool(config.get("enforce", True)),
            }
            for code, config in sorted(TENANT_RESIDENCY.items())
        },
        "default_region": DEFAULT_RESIDENCY_REGION,
        "metadata_field": RESIDENCY_METADATA_FIELD,
        "reasons": list(TENANT_RESIDENCY_REASONS),
        "region_by_tenant": {
            tenant_id: router.residency_for(tenant_id) for tenant_id in sorted(router._metadata)
        },
        "enforced_regions": sorted(
            code for code, config in TENANT_RESIDENCY.items() if config.get("enforce", True)
        ),
        "isolation": router.isolation_audit(),
        "note": (
            "residency is checked before an engine is handed out; a tenant pinned "
            "to a region cannot fall through to an unknown location, and a tenant "
            "on the shared engine is reported as unisolated rather than assumed safe"
        ),
    }