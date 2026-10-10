"""Admin controls for the external-brain provider pool.

Why these routes live under ``/chat/admin``
-------------------------------------------
The chat router already owns ``/chat/admin*``, and the authorization table
classifies that whole prefix as admin with one rule. Mounting here means every
route below inherits that gate *by prefix* -- the same reason
``app/routers/offers.py`` mounts without a prefix of its own. The alternative, a
new top-level prefix, would need a new ``AUTHZ_RULES`` row: a rule that matches a
route is fine, but a rule nobody remembers to add is a route served with no gate
at all, and inheriting an audited prefix is the safer default.

What an operator can do here, and why each exists
-------------------------------------------------
* **read the pool** (``GET``) -- which providers are configured, which models are
  cooling down and why, and what the next request would try first;
* **rotate a credential** (``POST .../credential``) -- the browser-session case,
  where a token expires and a redeploy is not an acceptable rotation story;
* **clear a credential** (``DELETE .../credential``) -- remove the stored
  override so the environment, if it has one, takes over again;
* **reset a cooldown** (``POST .../reset``) -- an operator who has just paid a
  bill or fixed a key should not have to wait out a cooldown the pool inferred;
* **force one failover** (``POST /failover``) -- answer a synthetic question
  through the real pool and return the attempt trace, so "is the fallback wired
  up?" is answerable without waiting for a quota to expire.

No response below contains a secret. The catalog reports that a credential is
present, never what it is, and the built provider URLs (one of which carries the
Gemini key as a query parameter) are never returned.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models
from app.services import ai_providers


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


router = APIRouter(prefix="/chat/admin/ai-providers", tags=["ai-providers"])


class CredentialIn(BaseModel):
    """A credential to install. The secret is write-only; it is never returned."""

    auth_mode: str = Field(default=ai_providers.AUTH_API_KEY, max_length=20)
    secret: str = Field(..., min_length=1, max_length=8192)
    label: str = Field(default="", max_length=80)


class ResetIn(BaseModel):
    """Narrow a cooldown reset to one model; ``None`` clears the provider."""

    model: Optional[str] = Field(default=None, max_length=120)


class FailoverIn(BaseModel):
    """A synthetic question to push through the real failover chain."""

    question: str = Field(default="What can you help me with?", max_length=500)
    budget_ms: float = Field(default=1200.0, gt=0, le=10000)


def _require_provider(provider: str) -> ai_providers.AiProvider:
    spec = ai_providers.PROVIDER_BY_NAME.get(provider)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown AI provider {provider!r}; known providers: "
                + ", ".join(ai_providers.provider_ids())
            ),
        )
    return spec


@router.get("")
async def list_ai_providers(
    current_user: models.User = Depends(deps.get_current_admin_user),
) -> dict[str, Any]:
    """The live pool: credentials present, cooldowns, and the next attempt list."""
    return ai_providers.POOL.snapshot()


@router.get("/catalog")
async def ai_provider_catalog(
    current_user: models.User = Depends(deps.get_current_admin_user),
) -> dict[str, Any]:
    """The static provider table plus live state, for planning a change."""
    return ai_providers.build_ai_providers_catalog(include_live=True)


@router.post("/{provider}/credential")
async def set_ai_provider_credential(
    provider: str,
    payload: CredentialIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
) -> dict[str, Any]:
    """Install or rotate a provider credential, encrypted at rest."""
    spec = _require_provider(provider)
    if payload.auth_mode not in spec.auth_modes:
        raise HTTPException(
            status_code=400,
            detail=(
                f"provider {provider!r} does not accept auth mode "
                f"{payload.auth_mode!r}; it accepts: "
                + ", ".join(spec.auth_modes)
            ),
        )

    row = (
        await db.execute(
            select(models.AiProviderCredential).where(
                models.AiProviderCredential.provider == provider
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = models.AiProviderCredential(provider=provider)
        db.add(row)
    row.label = payload.label or row.label or ""
    row.auth_mode = payload.auth_mode
    row.secret_encrypted = ai_providers.seal_secret(payload.secret)
    row.status = "active"
    # "database" and not the environment: a value written here has to stay
    # distinguishable from one a deploy injected, or the admin view cannot tell
    # a rotation from a restart.
    row.credential_source = "database"
    row.last_error = ""
    row.rotated_at = _utcnow()
    await db.commit()

    ai_providers.POOL.set_credential(
        provider,
        payload.auth_mode,
        payload.secret,
        source="database",
        label=payload.label,
    )
    return {
        "provider": provider,
        "configured": True,
        "auth_mode": payload.auth_mode,
        "credential_source": "database",
        "rotated_at": row.rotated_at.isoformat() if row.rotated_at else None,
    }


@router.delete("/{provider}/credential")
async def clear_ai_provider_credential(
    provider: str,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
) -> dict[str, Any]:
    """Forget the stored override. The environment, if any, takes over again."""
    _require_provider(provider)
    await db.execute(
        delete(models.AiProviderCredential).where(
            models.AiProviderCredential.provider == provider
        )
    )
    await db.commit()
    ai_providers.POOL.clear_credential(provider)
    return {
        "provider": provider,
        "configured": ai_providers.POOL.has_credential(provider),
        "credential_source": ai_providers.POOL.credential_source(provider),
    }


@router.post("/{provider}/reset")
async def reset_ai_provider_cooldowns(
    provider: str,
    payload: ResetIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
) -> dict[str, Any]:
    """Clear cooldowns in memory and on disk, optionally for one model."""
    _require_provider(provider)
    statement = delete(models.AiModelHealth).where(
        models.AiModelHealth.provider == provider
    )
    if payload.model:
        statement = statement.where(models.AiModelHealth.model == payload.model)
    result = await db.execute(statement)
    await db.commit()
    cleared = ai_providers.POOL.reset(provider, payload.model)
    return {
        "provider": provider,
        "model": payload.model,
        "cleared_in_memory": cleared,
        "cleared_rows": int(getattr(result, "rowcount", 0) or 0),
    }


@router.post("/failover")
async def trigger_ai_provider_failover(
    payload: FailoverIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
) -> dict[str, Any]:
    """Answer a synthetic question through the real pool and return the trace."""
    result = ai_providers.POOL.complete_traced(
        payload.question, {}, float(payload.budget_ms)
    )
    # Persist whatever the probe learned, so the cooldown it just created is
    # durable rather than a fact that disappears with the process.
    await ai_providers.persist_health(db)
    return result
