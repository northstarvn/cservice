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

Nothing here changes ``app.db`` or the existing ``get_db`` dependency.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db import _int_env, engine as default_engine

logger = logging.getLogger(__name__)

DEFAULT_TENANT = os.getenv("CSERVICE_DEFAULT_TENANT", "default")

# Multitenancy is opt-in: with an empty template and no declarations the router
# behaves exactly like the single-tenant backend (everything -> default engine).
TENANT_DSN_TEMPLATE = os.getenv("TENANT_DSN_TEMPLATE", "")
TENANT_POOL_SIZE = _int_env("TENANT_POOL_SIZE", 3)
TENANT_MAX_OVERFLOW = _int_env("TENANT_MAX_OVERFLOW", 5)
TENANT_POOL_TIMEOUT = _int_env("TENANT_POOL_TIMEOUT", 30)
TENANT_ECHO = os.getenv("TENANT_ECHO", "false").strip().lower() in {"1", "true", "yes", "on"}


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


class TenantRouter:
    """Routes database work to per-tenant async engines at runtime.

    Engines are created lazily on first use, pooled per tenant, and disposed on
    shutdown. A tenant without a dedicated DSN resolves to the shared default
    engine, so routing never isolates a tenant from data it legitimately shares.
    """

    def __init__(self, engine=default_engine):
        self._default_engine = engine
        self._engines: dict[str, Any] = {}
        self._specs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        tenant_id: str,
        dsn: str | None = None,
        *,
        pool_size: int | None = None,
        max_overflow: int | None = None,
        pool_timeout: int | None = None,
        echo: bool | None = None,
    ) -> Any:
        """Register (or return) the engine for ``tenant_id``.

        ``dsn`` may be omitted to use the template/declaration map; when no DSN
        is resolvable the tenant routes to the shared default engine.
        """
        tenant_id = (tenant_id or DEFAULT_TENANT).strip()
        if tenant_id in self._specs:
            return self._engines[tenant_id]
        dsn = dsn or parse_declared_tenants().get(tenant_id)
        if not dsn and TENANT_DSN_TEMPLATE and "{tenant}" in TENANT_DSN_TEMPLATE:
            dsn = TENANT_DSN_TEMPLATE.replace("{tenant}", tenant_id)
        async with self._lock:
            if tenant_id in self._specs:
                return self._engines[tenant_id]
            if not dsn:
                self._specs[tenant_id] = {
                    "tenant_id": tenant_id,
                    "dsn": None,
                    "mode": "shared_default",
                }
                self._engines[tenant_id] = self._default_engine
                return self._default_engine
            engine = create_async_engine(
                dsn,
                echo=TENANT_ECHO if echo is None else echo,
                pool_size=pool_size or TENANT_POOL_SIZE,
                max_overflow=max_overflow
                if max_overflow is not None
                else TENANT_MAX_OVERFLOW,
                pool_timeout=pool_timeout or TENANT_POOL_TIMEOUT,
            )
            self._engines[tenant_id] = engine
            self._specs[tenant_id] = {
                "tenant_id": tenant_id,
                "dsn": dsn,
                "mode": "dedicated_engine",
            }
            logger.info("Tenant engine registered: %s (dedicated)", tenant_id)
            return engine

    async def deregister(self, tenant_id: str) -> bool:
        """Drop a tenant's engine and dispose it (never touches the default)."""
        async with self._lock:
            engine = self._engines.pop(tenant_id, None)
            spec = self._specs.pop(tenant_id, None)
            if engine is None:
                return False
            if engine is not self._default_engine:
                await engine.dispose()
            logger.info("Tenant engine deregistered: %s", tenant_id)
            return bool(spec)

    async def engine_for(self, tenant_id: str | None = None) -> Any:
        """Resolve the engine for a tenant, registering it lazily if needed."""
        tenant_id = (tenant_id or DEFAULT_TENANT).strip()
        if tenant_id not in self._specs:
            await self.register(tenant_id)
        return self._engines[tenant_id]

    async def session_factory_for(self, tenant_id: str | None = None):
        """A fresh ``sessionmaker`` bound to the tenant's engine."""
        engine = await self.engine_for(tenant_id)
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Current routing table (tenant -> mode/dsn), for introspection."""
        return {
            tenant_id: {
                "mode": spec["mode"],
                "dsn": spec.get("dsn"),
            }
            for tenant_id, spec in sorted(self._specs.items())
        }

    async def dispose_all(self) -> None:
        """Dispose every created tenant engine (shutdown hook)."""
        async with self._lock:
            for tenant_id, engine in list(self._engines.items()):
                if engine is not self._default_engine:
                    await engine.dispose()
            self._engines.clear()
            self._specs.clear()


# Process-wide router; endpoints and workers share it.
registry = TenantRouter()


async def get_tenant_session(tenant_id: str | None = None) -> AsyncIterator[AsyncSession]:
    """Async session scoped to a tenant (prefer this over raw engines)."""
    factory = await registry.session_factory_for(tenant_id)
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_tenant_db(tenant_id: str) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency form of :func:`get_tenant_session`."""
    async for session in get_tenant_session(tenant_id):
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


def build_tenant_router_catalog() -> dict[str, object]:
    """Introspectable catalog for metadata endpoints."""
    declared = parse_declared_tenants()
    routed = bool(TENANT_DSN_TEMPLATE) or bool(declared)
    return {
        "default_tenant": DEFAULT_TENANT,
        "mode": "routed" if routed else "shared_default",
        "dsn_template_configured": bool(TENANT_DSN_TEMPLATE),
        "declared_tenants": declared,
        "registered": registry.snapshot(),
        "pool": {
            "size": TENANT_POOL_SIZE,
            "max_overflow": TENANT_MAX_OVERFLOW,
            "timeout": TENANT_POOL_TIMEOUT,
        },
        "lookup_order": ["X-Tenant-ID header", "tenant_id query param", "default tenant"],
    }