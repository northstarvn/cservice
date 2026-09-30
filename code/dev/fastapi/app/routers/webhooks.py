"""Webhook ingestion router: external systems push events into the pipeline.

This is the *reverse* of outbound enrichment — instead of calling out, the
system receives authenticated pushes from providers. The pipeline then
enriches and routes them like any other event.

Security
--------
- Each provider has a webhook secret in env (``WEBHOOK_SECRET_{PROVIDER}``).
- Signatures are verified with HMAC-SHA256 before the event is accepted.
- Replay protection: event IDs are deduplicated for 24 hours.
- Rate limiting: per-provider token bucket (config-driven).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app import deps, models
from app.high_throughput_pipeline import get_default_pipeline

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WEBHOOK_PROVIDERS: dict[str, dict[str, Any]] = {
    "kyc_provider": {
        "secret_env": "WEBHOOK_SECRET_KYC",
        "signature_header": "X-KYC-Signature",
        "event_id_header": "X-KYC-Event-Id",
        "rate_limit_per_minute": 60,
        "allowed_ips": [],
    },
    "firmographics": {
        "secret_env": "WEBHOOK_SECRET_FIRMO",
        "signature_header": "X-Firmo-Signature",
        "event_id_header": "X-Firmo-Event-Id",
        "rate_limit_per_minute": 30,
        "allowed_ips": [],
    },
    "fx_rates": {
        "secret_env": "WEBHOOK_SECRET_FX",
        "signature_header": "X-FX-Signature",
        "event_id_header": "X-FX-Event-Id",
        "rate_limit_per_minute": 120,
        "allowed_ips": [],
    },
    "weather": {
        "secret_env": "WEBHOOK_SECRET_WEATHER",
        "signature_header": "X-Weather-Signature",
        "event_id_header": "X-Weather-Event-Id",
        "rate_limit_per_minute": 60,
        "allowed_ips": [],
    },
    "shipping": {
        "secret_env": "WEBHOOK_SECRET_SHIPPING",
        "signature_header": "X-Shipping-Signature",
        "event_id_header": "X-Shipping-Event-Id",
        "rate_limit_per_minute": 100,
        "allowed_ips": [],
    },
}

# Replay protection: seen event IDs (24h TTL)
#
# The deque is the authority for *what is remembered*; the dict only carries
# timestamps. This inverts the previous arrangement, where the deque was
# written but never read and the dict -- the structure `_is_replay` actually
# consults -- had no bound at all. The status endpoints report
# `_seen_events.maxlen` as the cap, so a dict that grows past it made the
# reported capacity a fiction. Bounding the deque is what makes the number
# true, and evicting from the dict keeps the two in step.
#
# The residual is a real limitation rather than a bug: 10 000 entries is a
# fixed budget, so a flood of more than 10 000 distinct events inside the 24h
# window can age the oldest out and let a replay through. `maxlen` is
# `WEBHOOK_REPLAY_MEMORY` for that reason -- it is a deliberate memory /
# guarantee trade, tunable per deployment, not an implementation detail.
WEBHOOK_REPLAY_MEMORY = 10000
_seen_events: deque[str] = deque(maxlen=WEBHOOK_REPLAY_MEMORY)
_seen_events_ts: dict[str, float] = {}


def _prune_replay_memory() -> int:
    """Drop timestamps whose id has aged out of the deque. Returns the count.

    Called from the read and write paths rather than only from `_is_replay`,
    so the dict cannot keep growing while only a read-heavy workload is
    running. Without it, cleanup happened on exactly one call path, so a
    write-only flood left entries resident indefinitely.
    """
    live = set(_seen_events)
    stale = [event_id for event_id in _seen_events_ts if event_id not in live]
    for event_id in stale:
        _seen_events_ts.pop(event_id, None)
    return len(stale)

# Rate limiting per provider
_rate_buckets: dict[str, deque[float]] = {}


def _verify_signature(provider: str, body: bytes, signature: str) -> bool:
    """Verify HMAC-SHA256 signature."""
    cfg = WEBHOOK_PROVIDERS.get(provider, {})
    secret_env = cfg.get("secret_env", "")
    secret = os.getenv(secret_env, "")
    if not secret:
        logger.warning("No webhook secret configured for %s", provider)
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _check_rate_limit(provider: str) -> bool:
    """Token-bucket rate limit per provider."""
    cfg = WEBHOOK_PROVIDERS.get(provider, {})
    limit = int(cfg.get("rate_limit_per_minute", 60))
    now = time.monotonic()
    bucket = _rate_buckets.setdefault(provider, deque())
    # Remove entries older than 60s
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def _is_replay(event_id: str) -> bool:
    """Check if event ID was seen in the last 24 hours."""
    now = time.monotonic()
    # Time-based expiry first, then eviction-based cleanup. A 24h-old id is
    # not a replay even if it is still inside the deque's memory budget, and an
    # id evicted from the deque is not a replay regardless of its age.
    for eid in list(_seen_events_ts):
        if now - _seen_events_ts[eid] > 86400:
            _seen_events_ts.pop(eid, None)
    _prune_replay_memory()
    return event_id in _seen_events_ts


def _record_event(event_id: str) -> None:
    """Record event ID for replay protection.

    The append can evict the oldest id, so the timestamp map is reconciled on
    the way through. Doing it here as well as in `_is_replay` means the bound
    holds even when writes are the only traffic.
    """
    _seen_events.append(event_id)  # evicts the oldest once full
    _seen_events_ts[event_id] = time.monotonic()
    _prune_replay_memory()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/webhooks/{provider}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    provider: str,
    request: Request,
):
    """Receive a webhook push from an external provider.

    The event is verified, deduplicated, and enqueued into the high-throughput
    pipeline for async processing. No synchronous enrichment happens here.
    """
    if provider not in WEBHOOK_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown webhook provider: {provider}",
        )

    cfg = WEBHOOK_PROVIDERS[provider]

    # Rate limit
    if not _check_rate_limit(provider):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Webhook rate limit exceeded",
        )

    # Read body
    body = await request.body()

    # Verify signature
    signature = request.headers.get(cfg.get("signature_header", ""), "")
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing webhook signature",
        )
    if not _verify_signature(provider, body, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    # Parse payload
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON: {exc}",
        )

    # Replay protection
    event_id = request.headers.get(cfg.get("event_id_header", ""), "")
    if not event_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing event ID",
        )
    if _is_replay(event_id):
        return {"accepted": True, "event_id": event_id, "duplicate": True}
    _record_event(event_id)

    # Enqueue into pipeline
    pipeline = get_default_pipeline()
    event = pipeline.submit_event(
        kind=f"webhook.{provider}",
        severity="info",
        entity_type="webhook",
        entity_id=event_id,
        payload={
            "provider": provider,
            "event_id": event_id,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "data": payload,
        },
    )

    if event is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pipeline queue full; retry with backoff",
        )

    return {
        "accepted": True,
        "event_id": event_id,
        "duplicate": False,
    }


@router.get("/webhooks/{provider}/health")
async def webhook_health(provider: str):
    """Health check for a webhook provider."""
    if provider not in WEBHOOK_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown webhook provider: {provider}",
        )
    cfg = WEBHOOK_PROVIDERS[provider]
    secret = os.getenv(cfg.get("secret_env", ""), "")
    return {
        "provider": provider,
        "configured": bool(secret),
        "rate_limit_per_minute": cfg.get("rate_limit_per_minute", 60),
        "signature_header": cfg.get("signature_header", ""),
        "event_id_header": cfg.get("event_id_header", ""),
    }


@router.get("/webhooks/status")
async def webhook_status():
    """Status of all webhook providers."""
    return {
        "providers": {
            name: {
                "configured": bool(os.getenv(cfg.get("secret_env", ""), "")),
                "rate_limit_per_minute": cfg.get("rate_limit_per_minute", 60),
            }
            for name, cfg in WEBHOOK_PROVIDERS.items()
        },
        "replay_protection": {
            "seen_events": len(_seen_events),
            "max_events": _seen_events.maxlen,
        },
    }


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def build_webhook_catalog() -> dict[str, Any]:
    """Introspection payload for /meta/webhooks."""
    return {
        "providers": {
            name: {
                "secret_env": cfg.get("secret_env", ""),
                "signature_header": cfg.get("signature_header", ""),
                "event_id_header": cfg.get("event_id_header", ""),
                "rate_limit_per_minute": cfg.get("rate_limit_per_minute", 60),
                "configured": bool(os.getenv(cfg.get("secret_env", ""), "")),
            }
            for name, cfg in WEBHOOK_PROVIDERS.items()
        },
        "replay_protection": {
            "enabled": True,
            "ttl_hours": 24,
            "max_events": _seen_events.maxlen,
        },
        "endpoints": {
            "receive": "/webhooks/{provider}",
            "health": "/webhooks/{provider}/health",
            "status": "/webhooks/status",
        },
    }
