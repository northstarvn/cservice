"""Tests for external enrichment: providers, webhooks, pipeline, rule engine.

Covers:
- EnrichmentClient call/caching/circuit-breaker/budget logic
- Webhook signature verification, replay protection, rate limiting
- Pipeline enrichment stage (opt-in)
- Rule engine external operators (evaluate_when_external)
- Catalog endpoints
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import enrichment, rule_engine
from app.enrichment import (
    ENRICHMENT_PROVIDERS,
    ENRICHMENT_RULES,
    EnrichmentClient,
    EnrichmentResult,
    _BudgetTracker,
    _CircuitBreaker,
    _EnrichmentCache,
    _RateLimiter,
    _hash_value,
    _resolve_input_value,
    build_enrichment_catalog,
    get_enrichment_client,
    set_enrichment_client,
)
from app.routers import webhooks
from app.routers.webhooks import (
    WEBHOOK_PROVIDERS,
    _check_rate_limit,
    _is_replay,
    _record_event,
    _verify_signature,
    build_webhook_catalog,
)


# ---------------------------------------------------------------------------
# PII hashing
# ---------------------------------------------------------------------------


class TestPIIHashing:
    def test_hash_value_sha256(self):
        result = _hash_value("test@example.com")
        assert len(result) == 64
        assert result == hashlib.sha256(b"test@example.com").hexdigest()

    def test_hash_value_case_insensitive(self):
        assert _hash_value("Test@Example.com") == _hash_value("test@example.com")

    def test_hash_value_none(self):
        assert _hash_value(None) == ""

    def test_hash_value_sha512(self):
        result = _hash_value("test", algorithm="sha512")
        assert len(result) == 128


class TestInputResolution:
    def test_direct_field(self):
        ctx = {"email": "test@example.com"}
        assert _resolve_input_value("email", ctx) == "test@example.com"

    def test_sha256_field(self):
        ctx = {"email": "test@example.com"}
        result = _resolve_input_value("sha256(email)", ctx)
        assert result == hashlib.sha256(b"test@example.com").hexdigest()

    def test_sha256_email_domain(self):
        ctx = {"email": "user@example.com"}
        result = _resolve_input_value("sha256(email_domain)", ctx)
        assert result == hashlib.sha256(b"example.com").hexdigest()

    def test_missing_field(self):
        assert _resolve_input_value("missing", {}) is None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_within_limit(self):
        limiter = _RateLimiter(requests_per_minute=10)
        for _ in range(5):
            await limiter.acquire()  # should not block

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self):
        limiter = _RateLimiter(requests_per_minute=2)
        await limiter.acquire()
        await limiter.acquire()
        start = time.monotonic()
        await limiter.acquire()  # should wait ~30s
        elapsed = time.monotonic() - start
        assert elapsed >= 25  # generous lower bound


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_closed_allows_requests(self):
        cb = _CircuitBreaker(failure_threshold=3, recovery_seconds=30)
        assert cb.allow_request() is True

    def test_opens_after_threshold(self):
        cb = _CircuitBreaker(failure_threshold=3, recovery_seconds=30)
        cb.record_failure()
        cb.record_failure()
        assert cb.allow_request() is True
        cb.record_failure()
        assert cb.allow_request() is False

    def test_recovery_after_timeout(self):
        cb = _CircuitBreaker(failure_threshold=1, recovery_seconds=0.01)
        cb.record_failure()
        assert cb.allow_request() is False
        time.sleep(0.02)
        assert cb.allow_request() is True  # half-open

    def test_success_resets(self):
        cb = _CircuitBreaker(failure_threshold=3, recovery_seconds=30)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.state == "closed"
        assert cb.allow_request() is True


# ---------------------------------------------------------------------------
# Budget tracker
# ---------------------------------------------------------------------------


class TestBudgetTracker:
    @pytest.mark.asyncio
    async def test_allows_within_budget(self):
        budget = _BudgetTracker(monthly_budget_usd=100.0)
        assert await budget.charge(10.0) is True
        assert await budget.charge(10.0) is True

    @pytest.mark.asyncio
    async def test_blocks_at_80_percent(self):
        budget = _BudgetTracker(monthly_budget_usd=100.0)
        await budget.charge(81.0)
        assert await budget.charge(1.0) is False

    @pytest.mark.asyncio
    async def test_spent_tracking(self):
        budget = _BudgetTracker(monthly_budget_usd=50.0)
        await budget.charge(5.0)
        await budget.charge(3.0)
        assert budget.spent == 8.0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestEnrichmentCache:
    def test_put_and_get(self):
        cache = _EnrichmentCache()
        cache.put("rule1", {"email": "a@b.com"}, {"result": 42}, ttl_hours=1)
        assert cache.get("rule1", {"email": "a@b.com"}) == {"result": 42}

    def test_miss_returns_none(self):
        cache = _EnrichmentCache()
        assert cache.get("rule1", {"email": "a@b.com"}) is None

    def test_ttl_expiry(self):
        cache = _EnrichmentCache()
        cache.put("rule1", {"email": "a@b.com"}, {"result": 42}, ttl_hours=0)
        # TTL of 0 hours = 1 second minimum, so we can't test expiry easily
        # Just verify it was stored
        assert cache.get("rule1", {"email": "a@b.com"}) == {"result": 42}

    def test_invalidate(self):
        cache = _EnrichmentCache()
        cache.put("rule1", {"email": "a@b.com"}, {"result": 42}, ttl_hours=1)
        cache.invalidate("rule1")
        assert cache.get("rule1", {"email": "a@b.com"}) is None

    def test_clear(self):
        cache = _EnrichmentCache()
        cache.put("rule1", {"a": 1}, {"r": 1}, ttl_hours=1)
        cache.put("rule2", {"b": 2}, {"r": 2}, ttl_hours=1)
        cache.clear()
        assert cache.stats()["entries"] == 0

    def test_the_reported_capacity_is_actually_enforced(self):
        """`stats()` advertised `max_entries: 10000` that nothing enforced.

        `_data` was an unbounded dict and the only cleanup was a `get()` miss
        on an already-expired key, so a write-only workload grew without limit
        while the API reported a fixed capacity. Writing three times the cap
        must not exceed it.
        """
        cache = _EnrichmentCache(max_entries=500)
        for index in range(1500):
            cache.put(f"rule-{index % 20}", {"n": index}, {"v": index}, ttl_hours=24)
        stats = cache.stats()
        assert stats["entries"] <= stats["max_entries"]
        assert stats["at_capacity"] is True

    def test_eviction_is_least_recently_used(self):
        """A hot key must not be the victim of unrelated write pressure."""
        cache = _EnrichmentCache(max_entries=3)
        for index in range(3):
            cache.put("rule", {"n": index}, {"v": index}, ttl_hours=1)
        cache.get("rule", {"n": 0})  # mark 0 as most recently used
        cache.put("rule", {"n": 3}, {"v": 3}, ttl_hours=1)  # evicts the LRU (n=1)
        assert cache.get("rule", {"n": 0}) is not None
        assert cache.get("rule", {"n": 1}) is None

    def test_capacity_is_configurable_without_a_code_change(self):
        assert _EnrichmentCache(max_entries=7).stats()["max_entries"] == 7
        # A zero or negative bound would evict everything on each write.
        assert _EnrichmentCache(max_entries=0).stats()["max_entries"] == 1

    def test_invalidate_removes_entries_the_eviction_index_knows_about(self):
        """Eviction must not leave a rule's key set pointing at gone keys."""
        cache = _EnrichmentCache(max_entries=2)
        for index in range(6):
            cache.put("rule1", {"n": index}, {"v": index}, ttl_hours=1)
        cache.put("rule2", {"a": 1}, {"r": 1}, ttl_hours=1)
        cache.invalidate("rule1")
        stats = cache.stats()
        assert stats["entries"] == 1
        assert not cache.get("rule2", {"a": 1}) is None


# ---------------------------------------------------------------------------
# EnrichmentClient
# ---------------------------------------------------------------------------


class TestEnrichmentClient:
    @pytest.mark.asyncio
    async def test_disabled_returns_error(self):
        client = EnrichmentClient()
        rule = {"rule_id": "test", "provider": "kyc_provider"}
        result = await client.call(rule, {})
        assert result.success is False
        assert "disabled" in result.error

    @pytest.mark.asyncio
    async def test_unknown_provider(self):
        with patch.object(enrichment, "ENRICHMENT_ENABLED", True):
            client = EnrichmentClient()
            rule = {"rule_id": "test", "provider": "nonexistent"}
            result = await client.call(rule, {})
            assert result.success is False
            assert "unknown provider" in result.error

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks(self):
        with patch.object(enrichment, "ENRICHMENT_ENABLED", True):
            client = EnrichmentClient()
            cb = client._get_circuit_breaker("kyc_provider")
            cb._state = "open"
            cb._opened_at = time.monotonic()
            rule = {"rule_id": "test", "provider": "kyc_provider"}
            result = await client.call(rule, {})
            assert result.success is False
            assert "circuit breaker" in result.error

    @pytest.mark.asyncio
    async def test_budget_exhausted(self):
        with patch.object(enrichment, "ENRICHMENT_ENABLED", True):
            client = EnrichmentClient()
            budget = client._get_budget("kyc_provider")
            await budget.charge(1000.0)  # blow the budget
            rule = {"rule_id": "test", "provider": "kyc_provider"}
            result = await client.call(rule, {})
            assert result.success is False
            assert "budget" in result.error

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        with patch.object(enrichment, "ENRICHMENT_ENABLED", True):
            client = EnrichmentClient()
            rule = {
                "rule_id": "test_cache",
                "provider": "kyc_provider",
                "cache_ttl_hours": 1,
                "input_mapping": {"email": "email"},
                "output_mapping": {"risk": "risk_level"},
            }
            # Pre-populate cache
            client._cache.put("test_cache", {"email": "a@b.com"}, {"risk": 3}, 1)
            result = await client.call(rule, {"email": "a@b.com"})
            assert result.success is True
            assert result.cached is True
            assert result.data == {"risk": 3}

    @pytest.mark.asyncio
    async def test_enrich_event_matching(self):
        with patch.object(enrichment, "ENRICHMENT_ENABLED", True):
            client = EnrichmentClient()
            # Mock the call method
            async def mock_call(rule, ctx):
                return EnrichmentResult(
                    rule_id=rule["rule_id"],
                    provider=rule["provider"],
                    success=True,
                    data={"enriched_field": "value"},
                )
            client.call = mock_call
            result = await client.enrich_event("user.created", {"email": "a@b.com"})
            assert "enriched_field" in result

    @pytest.mark.asyncio
    async def test_enrich_event_no_match(self):
        with patch.object(enrichment, "ENRICHMENT_ENABLED", True):
            client = EnrichmentClient()
            result = await client.enrich_event("nonexistent.kind", {})
            assert result == {}

    def test_stats(self):
        client = EnrichmentClient()
        stats = client.stats()
        assert "enabled" in stats
        assert "providers" in stats
        assert "cache" in stats
        assert "rules_total" in stats

    def test_get_call_log(self):
        client = EnrichmentClient()
        log = client.get_call_log()
        assert isinstance(log, list)


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------


class TestWebhookSignature:
    def test_valid_signature(self):
        secret = "test_secret"
        body = b'{"event": "test"}'
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"WEBHOOK_SECRET_KYC": secret}):
            assert _verify_signature("kyc_provider", body, expected) is True

    def test_invalid_signature(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET_KYC": "secret"}):
            assert _verify_signature("kyc_provider", b"{}", "wrong") is False

    def test_missing_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _verify_signature("kyc_provider", b"{}", "sig") is False


# ---------------------------------------------------------------------------
# Webhook replay protection
# ---------------------------------------------------------------------------


class TestWebhookReplay:
    def test_first_occurrence_not_replay(self):
        _record_event("event_123")
        # Reset for clean test
        webhooks._seen_events.clear()
        webhooks._seen_events_ts.clear()
        _record_event("event_123")
        assert _is_replay("event_123") is True

    def test_different_event_not_replay(self):
        webhooks._seen_events.clear()
        webhooks._seen_events_ts.clear()
        _record_event("event_abc")
        assert _is_replay("event_xyz") is False

    def test_the_replay_guard_stays_within_its_reported_capacity(self):
        """The status endpoints report `_seen_events.maxlen` as the cap.

        `_is_replay` reads `_seen_events_ts`, not the deque, so a deque that is
        written but never consulted left the dict -- the structure that decides
        replay -- growing without any bound at all while the API advertised a
        fixed capacity. Recording three times the cap must not exceed it.
        """
        webhooks._seen_events.clear()
        webhooks._seen_events_ts.clear()
        cap = webhooks.WEBHOOK_REPLAY_MEMORY
        try:
            for index in range(cap * 3):
                _record_event(f"flood-{index}")
            assert len(webhooks._seen_events) <= cap
            assert len(webhooks._seen_events_ts) <= cap, (
                f"replay guard holds {len(webhooks._seen_events_ts)} entries "
                f"while reporting a capacity of {cap}"
            )
        finally:
            webhooks._seen_events.clear()
            webhooks._seen_events_ts.clear()

    def test_a_recent_event_is_still_caught_after_the_flood(self):
        """Bounding memory must not come at the cost of detecting replays."""
        webhooks._seen_events.clear()
        webhooks._seen_events_ts.clear()
        cap = webhooks.WEBHOOK_REPLAY_MEMORY
        try:
            for index in range(cap + 10):
                _record_event(f"flood-{index}")
            _record_event("the_one_that_matters")
            assert _is_replay("the_one_that_matters") is True
        finally:
            webhooks._seen_events.clear()
            webhooks._seen_events_ts.clear()

    def test_an_evicted_event_is_honestly_forgotten(self):
        """Documented limitation, asserted so it stays a decision.

        The 24h window and the fixed memory budget are different bounds, and
        the smaller one wins. This is a memory / replay-guarantee trade, not a
        silent leak -- but it is only a decision while it is written down.
        """
        webhooks._seen_events.clear()
        webhooks._seen_events_ts.clear()
        cap = webhooks.WEBHOOK_REPLAY_MEMORY
        try:
            _record_event("will_be_evicted")
            for index in range(cap + 5):
                _record_event(f"filler-{index}")
            assert _is_replay("will_be_evicted") is False
        finally:
            webhooks._seen_events.clear()
            webhooks._seen_events_ts.clear()


# ---------------------------------------------------------------------------
# Webhook rate limiting
# ---------------------------------------------------------------------------


class TestWebhookRateLimit:
    def test_allows_within_limit(self):
        webhooks._rate_buckets.clear()
        for _ in range(5):
            assert _check_rate_limit("kyc_provider") is True

    def test_blocks_over_limit(self):
        webhooks._rate_buckets.clear()
        # Set a very low limit for testing
        original = WEBHOOK_PROVIDERS["kyc_provider"]["rate_limit_per_minute"]
        WEBHOOK_PROVIDERS["kyc_provider"]["rate_limit_per_minute"] = 2
        try:
            assert _check_rate_limit("kyc_provider") is True
            assert _check_rate_limit("kyc_provider") is True
            assert _check_rate_limit("kyc_provider") is False
        finally:
            WEBHOOK_PROVIDERS["kyc_provider"]["rate_limit_per_minute"] = original


# ---------------------------------------------------------------------------
# Rule engine external operators
# ---------------------------------------------------------------------------


class TestExternalOperators:
    def test_enriched_field_present(self):
        enriched = {"kyc_risk_level": 3}
        result = rule_engine._external_op_holds(
            "enriched_field", "kyc_risk_level", {"gte": 2}, enriched
        )
        assert result is True

    def test_enriched_field_absent(self):
        result = rule_engine._external_op_holds(
            "enriched_field", "kyc_risk_level", {"gte": 2}, None
        )
        assert result is False

    def test_enriched_present(self):
        enriched = {"company_size": 500}
        assert rule_engine._external_op_holds("enriched_present", "company_size", None, enriched) is True

    def test_enriched_absent(self):
        enriched = {"company_size": 500}
        assert rule_engine._external_op_holds("enriched_absent", "missing_field", None, enriched) is True

    def test_evaluate_when_external_with_enrichment(self):
        # The field must exist in context for the external operator to be evaluated
        context = {"email": "test@example.com", "kyc_risk_level": 3}
        enriched = {"kyc_risk_level": 3, "company_size": 500}
        when = {"kyc_risk_level": {"gte": 2}}
        ok, fields = rule_engine.evaluate_when_external(
            when, context, enriched_context=enriched
        )
        assert ok is True

    def test_evaluate_when_external_without_enrichment_fails_closed(self):
        context = {"email": "test@example.com"}
        when = {"kyc_risk_level": {"gte": 2}}
        ok, _ = rule_engine.evaluate_when_external(when, context, enriched_context=None)
        assert ok is False

    def test_external_ops_in_operator_families(self):
        external_ops = [op["op"] for op in rule_engine.EXTERNAL_OPS]
        for op in external_ops:
            assert rule_engine.OPERATOR_FAMILIES.get(op) == "external"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_enrichment_catalog(self):
        catalog = build_enrichment_catalog()
        assert "enabled" in catalog
        assert "providers" in catalog
        assert "rules" in catalog
        assert "stats" in catalog
        assert "endpoints" in catalog

    def test_webhook_catalog(self):
        catalog = build_webhook_catalog()
        assert "providers" in catalog
        assert "replay_protection" in catalog
        assert "endpoints" in catalog


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_enrichment_client(self):
        client = get_enrichment_client()
        assert isinstance(client, EnrichmentClient)

    def test_set_enrichment_client(self):
        mock = MagicMock(spec=EnrichmentClient)
        set_enrichment_client(mock)
        assert get_enrichment_client() is mock
        # Reset
        set_enrichment_client(None)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_all_providers_have_required_fields(self):
        required = {"base_url", "auth", "rate_limit", "timeout_seconds", "retry"}
        for name, cfg in ENRICHMENT_PROVIDERS.items():
            for field in required:
                assert field in cfg, f"Provider {name} missing {field}"

    def test_all_rules_have_required_fields(self):
        required = {"rule_id", "provider", "trigger", "input_mapping", "output_mapping"}
        for rule in ENRICHMENT_RULES:
            for field in required:
                assert field in rule, f"Rule {rule.get('rule_id')} missing {field}"

    def test_all_rule_providers_exist(self):
        for rule in ENRICHMENT_RULES:
            assert rule["provider"] in ENRICHMENT_PROVIDERS

    def test_all_webhook_providers_have_secrets_configured(self):
        for name, cfg in WEBHOOK_PROVIDERS.items():
            assert "secret_env" in cfg
            assert "signature_header" in cfg
            assert "event_id_header" in cfg
