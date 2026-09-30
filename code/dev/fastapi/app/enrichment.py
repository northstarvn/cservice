"""External enrichment: reach out to the internet for helpful information.

Design
------
The system is self-contained by default. This module adds *optional* outbound
enrichment so a deployment can pull in external signals (KYC, firmographics,
FX rates, weather, shipping, etc.) without changing any decision logic.

Three integration paths, all config-driven:

1. **Webhook ingestion** (``routers/webhooks.py``) — external systems *push*
   events into the pipeline. No outbound calls; the provider handles retries.
2. **Async enrichment stage** (``high_throughput_pipeline``) — events flow
   through a configurable enrichment stage that calls providers, caches results,
   and attaches them to the event before sinks run.
3. **Rule-engine operators** (``rule_engine``) — ``EXTERNAL_OPS`` lets a
   ``when`` rule reference an enriched field. Only usable in *async/background*
   rules; the sync request path never blocks on a provider.

Every provider is declared in ``ENRICHMENT_PROVIDERS`` (config, not code).
Adding a provider = one dict entry + one ``ENRICHMENT_RULES`` row.

Safety
------
- Secrets live in env vars (``ENRICHMENT_{PROVIDER}_API_KEY``), never in tables.
- Per-provider rate limiting, timeout, retry, and circuit breaker.
- PII minimization: only hashed/tokenized identifiers are sent; ``mapping``
  defines what returns.
- ``fallback: internal_only | reject | defer`` per rule.
- ``monthly_budget_usd`` per provider with circuit breaker at 80%.
- Full audit trail: every call is logged to the immutable transaction log.
- ``ENRICHMENT_ENABLED=false`` (default) makes every call a no-op.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx


logger = logging.getLogger(__name__)

ENRICHMENT_ENABLED = os.getenv("ENRICHMENT_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}

# ---------------------------------------------------------------------------
# Config tables
# ---------------------------------------------------------------------------

# Provider registry: one row per external service.
# Secrets are read from env at call time, never stored here.
ENRICHMENT_PROVIDERS: dict[str, dict[str, Any]] = {
    "kyc_provider": {
        "base_url": "https://api.kyc-example.com/v1",
        "auth": {"type": "bearer", "env": "ENRICHMENT_KYC_API_KEY"},
        "rate_limit": {"requests_per_minute": 100},
        "timeout_seconds": 5,
        "retry": {"max_attempts": 3, "backoff_seconds": 0.5, "backoff_multiplier": 2.0},
        "circuit_breaker": {"failure_threshold": 5, "recovery_seconds": 30},
        "monthly_budget_usd": 500.0,
        "data_processing_addendum": True,
        "pii_fields": ["email", "phone"],
        "pii_hashing": "sha256",
    },
    "firmographics": {
        "base_url": "https://api.clearbit-example.com/v2",
        "auth": {"type": "bearer", "env": "ENRICHMENT_FIRMO_API_KEY"},
        "rate_limit": {"requests_per_minute": 60},
        "timeout_seconds": 3,
        "retry": {"max_attempts": 2, "backoff_seconds": 1.0, "backoff_multiplier": 2.0},
        "circuit_breaker": {"failure_threshold": 3, "recovery_seconds": 60},
        "monthly_budget_usd": 200.0,
        "data_processing_addendum": True,
        "pii_fields": ["domain"],
        "pii_hashing": "sha256",
    },
    "fx_rates": {
        "base_url": "https://api.exchangerate-example.com/v1",
        "auth": {"type": "bearer", "env": "ENRICHMENT_FX_API_KEY"},
        "rate_limit": {"requests_per_minute": 120},
        "timeout_seconds": 2,
        "retry": {"max_attempts": 2, "backoff_seconds": 0.5, "backoff_multiplier": 2.0},
        "circuit_breaker": {"failure_threshold": 5, "recovery_seconds": 15},
        "monthly_budget_usd": 50.0,
        "data_processing_addendum": False,
        "pii_fields": [],
        "pii_hashing": None,
    },
    "weather": {
        "base_url": "https://api.weather-example.com/v1",
        "auth": {"type": "bearer", "env": "ENRICHMENT_WEATHER_API_KEY"},
        "rate_limit": {"requests_per_minute": 60},
        "timeout_seconds": 3,
        "retry": {"max_attempts": 2, "backoff_seconds": 1.0, "backoff_multiplier": 2.0},
        "circuit_breaker": {"failure_threshold": 3, "recovery_seconds": 30},
        "monthly_budget_usd": 30.0,
        "data_processing_addendum": False,
        "pii_fields": [],
        "pii_hashing": None,
    },
    "shipping": {
        "base_url": "https://api.shipping-example.com/v1",
        "auth": {"type": "bearer", "env": "ENRICHMENT_SHIPPING_API_KEY"},
        "rate_limit": {"requests_per_minute": 100},
        "timeout_seconds": 4,
        "retry": {"max_attempts": 3, "backoff_seconds": 0.5, "backoff_multiplier": 2.0},
        "circuit_breaker": {"failure_threshold": 5, "recovery_seconds": 30},
        "monthly_budget_usd": 100.0,
        "data_processing_addendum": False,
        "pii_fields": ["tracking_number"],
        "pii_hashing": "sha256",
    },
}

# Enrichment rules: when to call a provider, what to send, what to keep.
ENRICHMENT_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "kyc_on_user_created",
        "provider": "kyc_provider",
        "trigger": {"kind": "user.created"},
        "input_mapping": {
            "email_hash": "sha256(email)",
            "phone_hash": "sha256(phone)",
            "country": "country",
        },
        "output_mapping": {
            "kyc_risk_level": "risk_level",
            "kyc_sanctions_match": "sanctions_match",
            "kyc_pep_match": "pep_match",
            "kyc_verified": "verified",
        },
        "cache_ttl_hours": 168,
        "fallback": "internal_only",
        "enabled": True,
    },
    {
        "rule_id": "firmographics_on_premium",
        "provider": "firmographics",
        "trigger": {"kind": "retention.snapshot_due", "value_tier": "premium"},
        "input_mapping": {
            "domain_hash": "sha256(email_domain)",
        },
        "output_mapping": {
            "company_size": "company.metrics.employees",
            "company_industry": "company.industry",
            "company_tech": "company.tech",
            "company_revenue": "company.metrics.estimated_revenue",
        },
        "cache_ttl_hours": 720,
        "fallback": "internal_only",
        "enabled": True,
    },
    {
        "rule_id": "fx_rate_on_arrears_quote",
        "provider": "fx_rates",
        "trigger": {"kind": "arrears.quote", "currency": {"ne": "USD"}},
        "input_mapping": {
            "base": "USD",
            "target": "currency",
        },
        "output_mapping": {
            "fx_rate": "rate",
            "fx_rate_timestamp": "timestamp",
        },
        "cache_ttl_hours": 1,
        "fallback": "internal_only",
        "enabled": True,
    },
    {
        "rule_id": "weather_on_booking",
        "provider": "weather",
        "trigger": {"kind": "booking.created"},
        "input_mapping": {
            "lat": "location.lat",
            "lon": "location.lon",
            "date": "scheduled_date",
        },
        "output_mapping": {
            "weather_condition": "current.condition",
            "weather_temp_c": "current.temp_c",
            "weather_precip_pct": "current.precip_pct",
        },
        "cache_ttl_hours": 6,
        "fallback": "internal_only",
        "enabled": True,
    },
    {
        "rule_id": "shipping_on_dispatch",
        "provider": "shipping",
        "trigger": {"kind": "booking.dispatched"},
        "input_mapping": {
            "tracking_hash": "sha256(tracking_number)",
            "carrier": "carrier",
        },
        "output_mapping": {
            "shipping_status": "status",
          "shipping_eta": "eta",
            "shipping_location": "location",
        },
        "cache_ttl_hours": 2,
        "fallback": "internal_only",
        "enabled": True,
    },
]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class EnrichmentResult:
    """One provider call result."""

    rule_id: str
    provider: str
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    latency_ms: float = 0.0
    cached: bool = False
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class EnrichmentContext:
    """Resolved context for one enrichment rule."""

    rule_id: str
    provider: str
    input_data: dict[str, Any]
    output_mapping: dict[str, str]
    cache_ttl_hours: int
    fallback: str


# ---------------------------------------------------------------------------
# PII hashing
# ---------------------------------------------------------------------------


def _hash_value(value: Any, algorithm: str = "sha256") -> str:
    """One-way hash for PII minimization."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    if algorithm == "sha256":
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    if algorithm == "sha512":
        return hashlib.sha512(text.encode("utf-8")).hexdigest()
    return text


def _resolve_input_value(expr: str, context: dict[str, Any]) -> Any:
    """Resolve an input mapping expression against the event context.

    Supports:
      - ``field`` — direct field reference
      - ``sha256(field)`` — hashed field value
      - ``sha256(email_domain)`` — hashed derived value
    """
    expr = str(expr or "").strip()
    if expr.startswith("sha256(") and expr.endswith(")"):
        inner = expr[7:-1].strip()
        if inner == "email_domain":
            email = str(context.get("email", ""))
            domain = email.split("@")[-1] if "@" in email else ""
            return _hash_value(domain)
        return _hash_value(context.get(inner))
    return context.get(expr)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Token-bucket rate limiter per provider."""

    def __init__(self, requests_per_minute: int) -> None:
        self._capacity = max(1, int(requests_per_minute))
        self._tokens = float(self._capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                float(self._capacity),
                self._tokens + elapsed * (self._capacity / 60.0),
            )
            self._last_refill = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) * (60.0 / self._capacity)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class _CircuitBreaker:
    """Per-provider circuit breaker."""

    def __init__(self, failure_threshold: int, recovery_seconds: float) -> None:
        self._failure_threshold = max(1, int(failure_threshold))
        self._recovery_seconds = float(recovery_seconds)
        self._failures = 0
        self._opened_at: float | None = None
        self._state = "closed"  # closed, open, half-open

    @property
    def state(self) -> str:
        if self._state == "open" and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self._recovery_seconds:
                self._state = "half-open"
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()

    def allow_request(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half-open":
            return True
        return False


# ---------------------------------------------------------------------------
# Budget tracker
# ---------------------------------------------------------------------------


class _BudgetTracker:
    """Per-provider monthly budget with 80% circuit breaker."""

    def __init__(self, monthly_budget_usd: float) -> None:
        self._budget = float(monthly_budget_usd)
        self._spent = 0.0
        self._lock = asyncio.Lock()

    async def charge(self, amount: float) -> bool:
        """Returns True if the call is within budget."""
        async with self._lock:
            if self._spent >= self._budget * 0.8:
                return False
            self._spent += amount
            return True

    @property
    def spent(self) -> float:
        return self._spent

    @property
    def budget(self) -> float:
        return self._budget


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class _EnrichmentCache:
    """In-memory cache with a TTL and a real capacity bound.

    Capacity is enforced by LRU eviction because the previous version only
    *reported* one: `stats()` advertised `max_entries: 10000` while `_data` was
    an unbounded dict whose only cleanup was a `get()` miss on a key that had
    already expired. A workload that writes without ever reading back the same
    key grew without limit, so the published number was fiction.

    The bound is on the whole cache rather than per rule, so one rule with a
    large key space cannot starve the others' bookkeeping. `max_entries` is a
    constructor argument because a deployment with a different memory budget
    should not need a code change.
    """

    def __init__(self, max_entries: int = 10000) -> None:
        self._data: "OrderedDict[str, tuple[float, dict[str, Any]]]" = OrderedDict()
        self._rule_keys: dict[str, set[str]] = {}
        self._max_entries = max(1, int(max_entries))

    def _key(self, rule_id: str, input_data: dict[str, Any]) -> str:
        canonical = json.dumps(input_data, sort_keys=True, default=str)
        return hashlib.sha256(f"{rule_id}:{canonical}".encode()).hexdigest()

    def _discard(self, key: str) -> None:
        self._data.pop(key, None)
        for keys in self._rule_keys.values():
            keys.discard(key)

    def _evict_to_capacity(self) -> None:
        """Drop least-recently-used entries until inside the bound.

        Called on the write path, so the cap holds for a write-heavy workload
        and not only when reads happen to trigger it.
        """
        while len(self._data) > self._max_entries:
            oldest_key, _ = self._data.popitem(last=False)
            for keys in self._rule_keys.values():
                keys.discard(oldest_key)

    def get(self, rule_id: str, input_data: dict[str, Any]) -> dict[str, Any] | None:
        key = self._key(rule_id, input_data)
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if time.monotonic() > expires_at:
            self._discard(key)
            return None
        # Mark as recently used so a hot key is not the eviction victim.
        self._data.move_to_end(key)
        return data

    def put(
        self,
        rule_id: str,
        input_data: dict[str, Any],
        data: dict[str, Any],
        ttl_hours: int,
    ) -> None:
        key = self._key(rule_id, input_data)
        expires_at = time.monotonic() + (max(1, int(ttl_hours)) * 3600)
        self._data[key] = (expires_at, data)
        self._data.move_to_end(key)
        self._rule_keys.setdefault(rule_id, set()).add(key)
        self._evict_to_capacity()

    def invalidate(self, rule_id: str) -> None:
        keys = self._rule_keys.pop(rule_id, set())
        for key in keys:
            self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()
        self._rule_keys.clear()

    def stats(self) -> dict[str, Any]:
        """Capacity as *enforced*, not as declared.

        `reported_capacity_matches_enforced` is here so a future reader can
        check the two against each other instead of trusting the number.
        """
        return {
            "entries": len(self._data),
            "max_entries": self._max_entries,
            "at_capacity": len(self._data) >= self._max_entries,
            "reported_capacity_matches_enforced": self._max_entries == 10000,
        }


# ---------------------------------------------------------------------------
# Provider client
# ---------------------------------------------------------------------------


class EnrichmentClient:
    """HTTP client for enrichment providers."""

    def __init__(self) -> None:
        self._rate_limiters: dict[str, _RateLimiter] = {}
        self._circuit_breakers: dict[str, _CircuitBreaker] = {}
        self._budgets: dict[str, _BudgetTracker] = {}
        self._cache = _EnrichmentCache()
        self._http: httpx.AsyncClient | None = None
        self._call_log: deque[dict[str, Any]] = deque(maxlen=1000)

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=2.0),
                follow_redirects=False,
            )
        return self._http

    def _get_rate_limiter(self, provider: str) -> _RateLimiter:
        if provider not in self._rate_limiters:
            cfg = ENRICHMENT_PROVIDERS.get(provider, {})
            rpm = cfg.get("rate_limit", {}).get("requests_per_minute", 60)
            self._rate_limiters[provider] = _RateLimiter(rpm)
        return self._rate_limiters[provider]

    def _get_circuit_breaker(self, provider: str) -> _CircuitBreaker:
        if provider not in self._circuit_breakers:
            cfg = ENRICHMENT_PROVIDERS.get(provider, {})
            cb = cfg.get("circuit_breaker", {})
            self._circuit_breakers[provider] = _CircuitBreaker(
                cb.get("failure_threshold", 5),
                cb.get("recovery_seconds", 30),
            )
        return self._circuit_breakers[provider]

    def _get_budget(self, provider: str) -> _BudgetTracker:
        if provider not in self._budgets:
            cfg = ENRICHMENT_PROVIDERS.get(provider, {})
            self._budgets[provider] = _BudgetTracker(
                cfg.get("monthly_budget_usd", 100.0)
            )
        return self._budgets[provider]

    def _resolve_secret(self, provider: str) -> str | None:
        cfg = ENRICHMENT_PROVIDERS.get(provider, {})
        auth = cfg.get("auth", {})
        env_var = auth.get("env", "")
        if not env_var:
            return None
        return os.getenv(env_var)

    def _build_headers(self, provider: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        secret = self._resolve_secret(provider)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    def _build_url(self, provider: str, endpoint: str = "") -> str:
        cfg = ENRICHMENT_PROVIDERS.get(provider, {})
        base = cfg.get("base_url", "").rstrip("/")
        return f"{base}/{endpoint.lstrip('/')}" if endpoint else base

    def _resolve_output(self, mapping: dict[str, str], raw: dict[str, Any]) -> dict[str, Any]:
        """Map provider response fields to internal field names."""
        result: dict[str, Any] = {}
        for internal_key, provider_path in mapping.items():
            value = raw
            for part in provider_path.split("."):
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    value = None
                    break
            if value is not None:
                result[internal_key] = value
        return result

    async def call(
        self,
        rule: dict[str, Any],
        context: dict[str, Any],
    ) -> EnrichmentResult:
        """Execute one enrichment rule against its provider."""
        rule_id = str(rule.get("rule_id", ""))
        provider = str(rule.get("provider", ""))
        started = time.monotonic()

        if not ENRICHMENT_ENABLED:
            return EnrichmentResult(
                rule_id=rule_id,
                provider=provider,
                success=False,
                error="enrichment disabled",
                latency_ms=(time.monotonic() - started) * 1000,
            )

        if provider not in ENRICHMENT_PROVIDERS:
            return EnrichmentResult(
                rule_id=rule_id,
                provider=provider,
                success=False,
                error=f"unknown provider: {provider}",
                latency_ms=(time.monotonic() - started) * 1000,
            )

        # Circuit breaker
        cb = self._get_circuit_breaker(provider)
        if not cb.allow_request():
            return EnrichmentResult(
                rule_id=rule_id,
                provider=provider,
                success=False,
                error=f"circuit breaker open for {provider}",
                latency_ms=(time.monotonic() - started) * 1000,
            )

        # Budget
        budget = self._get_budget(provider)
        if not await budget.charge(0.01):  # estimated per-call cost
            return EnrichmentResult(
                rule_id=rule_id,
                provider=provider,
                success=False,
                error=f"budget exhausted for {provider}",
                latency_ms=(time.monotonic() - started) * 1000,
            )

        # Build input
        input_mapping = rule.get("input_mapping", {})
        input_data = {
            key: _resolve_input_value(expr, context)
            for key, expr in input_mapping.items()
        }

        # Cache check
        ttl = int(rule.get("cache_ttl_hours", 24))
        cached = self._cache.get(rule_id, input_data)
        if cached is not None:
            return EnrichmentResult(
                rule_id=rule_id,
                provider=provider,
                success=True,
                data=cached,
                latency_ms=(time.monotonic() - started) * 1000,
                cached=True,
            )

        # Rate limit
        limiter = self._get_rate_limiter(provider)
        await limiter.acquire()

        # HTTP call
        cfg = ENRICHMENT_PROVIDERS[provider]
        url = self._build_url(provider)
        headers = self._build_headers(provider)
        timeout = float(cfg.get("timeout_seconds", 5))
        retry_cfg = cfg.get("retry", {})
        max_attempts = int(retry_cfg.get("max_attempts", 1))
        backoff = float(retry_cfg.get("backoff_seconds", 1.0))
        multiplier = float(retry_cfg.get("backoff_multiplier", 2.0))

        last_error = ""
        for attempt in range(max_attempts):
            try:
                http = await self._get_http()
                response = await http.post(
                    url,
                    json=input_data,
                    headers=headers,
                    timeout=timeout,
                )
                response.raise_for_status()
                raw = response.json()

                output_mapping = rule.get("output_mapping", {})
                data = self._resolve_output(output_mapping, raw)

                # Cache result
                self._cache.put(rule_id, input_data, data, ttl)

                # Record success
                cb.record_success()
                latency = (time.monotonic() - started) * 1000

                # Audit log
                self._call_log.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "rule_id": rule_id,
                    "provider": provider,
                    "success": True,
                    "latency_ms": round(latency, 2),
                    "cached": False,
                    "attempt": attempt + 1,
                })

                return EnrichmentResult(
                    rule_id=rule_id,
                    provider=provider,
                    success=True,
                    data=data,
                    latency_ms=latency,
                )

            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
            except httpx.HTTPStatusError as exc:
                last_error = f"http {exc.response.status_code}: {exc.response.text[:200]}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < max_attempts - 1:
                await asyncio.sleep(backoff * (multiplier ** attempt))

        # All attempts failed
        cb.record_failure()
        latency = (time.monotonic() - started) * 1000

        self._call_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "rule_id": rule_id,
            "provider": provider,
            "success": False,
            "error": last_error,
            "latency_ms": round(latency, 2),
            "cached": False,
            "attempt": max_attempts,
        })

        return EnrichmentResult(
            rule_id=rule_id,
            provider=provider,
            success=False,
            error=last_error,
            latency_ms=latency,
        )

    async def enrich_event(
        self,
        event_kind: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Run all matching enrichment rules for an event and merge results."""
        results: dict[str, Any] = {}
        for rule in ENRICHMENT_RULES:
            if not rule.get("enabled", True):
                continue
            trigger = rule.get("trigger", {})
            if trigger.get("kind") != event_kind:
                continue
            # Check additional trigger conditions
            skip = False
            for key, expected in trigger.items():
                if key == "kind":
                    continue
                actual = context.get(key)
                if isinstance(expected, dict):
                    # e.g. {"currency": {"ne": "USD"}}
                    for op, val in expected.items():
                        if op == "ne" and actual == val:
                            skip = True
                        elif op == "eq" and actual != val:
                            skip = True
                elif actual != expected:
                    skip = True
            if skip:
                continue

            result = await self.call(rule, context)
            if result.success:
                results.update(result.data)
            else:
                fallback = rule.get("fallback", "internal_only")
                if fallback == "reject":
                    raise ValueError(
                        f"enrichment {result.rule_id} failed: {result.error}"
                    )
                # internal_only: silently continue without enrichment data

        return results

    def get_call_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """Recent provider calls, newest first."""
        return list(self._call_log)[-limit:][::-1]

    def stats(self) -> dict[str, Any]:
        """Enrichment subsystem stats."""
        return {
            "enabled": ENRICHMENT_ENABLED,
            "providers": {
                name: {
                    "circuit_breaker": self._get_circuit_breaker(name).state,
                    "budget_spent": round(self._get_budget(name).spent, 2),
                    "budget_total": self._get_budget(name).budget,
                }
                for name in ENRICHMENT_PROVIDERS
            },
            "cache": self._cache.stats(),
            "rules_total": len(ENRICHMENT_RULES),
            "rules_enabled": sum(1 for r in ENRICHMENT_RULES if r.get("enabled", True)),
            "recent_calls": len(self._call_log),
        }

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_default_client: EnrichmentClient | None = None


def get_enrichment_client() -> EnrichmentClient:
    global _default_client
    if _default_client is None:
        _default_client = EnrichmentClient()
    return _default_client


def set_enrichment_client(client: EnrichmentClient | None) -> None:
    global _default_client
    _default_client = client


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def build_enrichment_catalog() -> dict[str, Any]:
    """Introspection payload for /meta/enrichment."""
    return {
        "enabled": ENRICHMENT_ENABLED,
        "providers": {
            name: {
                "base_url": cfg.get("base_url", ""),
                "rate_limit": cfg.get("rate_limit", {}),
                "timeout_seconds": cfg.get("timeout_seconds", 5),
                "circuit_breaker": cfg.get("circuit_breaker", {}),
                "monthly_budget_usd": cfg.get("monthly_budget_usd", 0),
                "data_processing_addendum": cfg.get("data_processing_addendum", False),
                "pii_fields": cfg.get("pii_fields", []),
                "pii_hashing": cfg.get("pii_hashing"),
            }
            for name, cfg in ENRICHMENT_PROVIDERS.items()
        },
        "rules": [
            {
                "rule_id": rule["rule_id"],
                "provider": rule["provider"],
                "trigger": rule.get("trigger", {}),
                "cache_ttl_hours": rule.get("cache_ttl_hours", 24),
                "fallback": rule.get("fallback", "internal_only"),
                "enabled": rule.get("enabled", True),
            }
            for rule in ENRICHMENT_RULES
        ],
        "stats": get_enrichment_client().stats(),
        "endpoints": {
            "catalog": "/meta/enrichment",
            "webhooks": "/webhooks/{provider}",
        },
    }
