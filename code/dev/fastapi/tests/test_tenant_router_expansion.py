"""Tests for Stage 1A: multi-tenant database routing + partition lifecycle.

Covers the polymorphic base models (``app/model_bases.py``), the runtime
tenant connection router (``app/tenant_router.py``), and the dynamic partition
lifecycle workers (``app/partition_manager.py``), plus the metadata surfaces
that document them.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import model_bases, models, partition_manager, tenant_router


# --- polymorphic base models --------------------------------------------------


def test_timestamp_mixin_is_isolated_in_model_bases():
    # Entities keep deriving from the shared, isolated base definition.
    assert models.TimestampMixin is model_bases.TimestampMixin


def test_security_event_polymorphic_hierarchy():
    assert model_bases.SecurityEvent.__tablename__ == "security_events"
    # Single-table inheritance: discriminators on one table.
    assert model_bases.AuthenticationSecurityEvent.__tablename__ == "security_events"
    assert model_bases.RiskSecurityEvent.__tablename__ == "security_events"
    assert model_bases.AccessSecurityEvent.__tablename__ == "security_events"

    mapper = model_bases.AuthenticationSecurityEvent.__mapper__
    assert mapper.polymorphic_identity == "authentication"
    assert mapper.inherits is model_bases.SecurityEvent.__mapper__

    assert model_bases.security_event_family("risk") is model_bases.RiskSecurityEvent
    assert model_bases.security_event_family("unknown") is model_bases.SecurityEvent


def test_security_event_carries_tenant_and_partition_scoping():
    cols = {col.name for col in model_bases.SecurityEvent.__table__.columns}
    assert {"tenant_id", "partition_key", "event_kind", "risk_score"}.issubset(cols)


def test_model_bases_catalog_is_introspectable():
    catalog = model_bases.build_model_bases_catalog()
    assert set(catalog["families"]) == {"authentication", "risk", "access"}
    assert catalog["discriminator"] == "event_kind"


# --- partition lifecycle ------------------------------------------------------


def test_period_key_and_end_formats():
    moment = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    assert partition_manager.period_key("daily", moment) == "2026-09-25"
    assert partition_manager.period_key("monthly", moment) == "2026-09"
    assert partition_manager.period_key("weekly", moment) == "2026-W38"
    with pytest.raises(ValueError):
        partition_manager.period_key("yearly", moment)

    assert partition_manager.period_end("monthly", "2026-09") == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert partition_manager.period_end("monthly", "2026-12") == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert partition_manager.period_end("daily", "2026-09-25") == datetime(2026, 9, 26, tzinfo=timezone.utc)


def test_partition_name_and_ddl_are_config_driven():
    policy = partition_manager.PARTITION_POLICIES[0]
    key = partition_manager.period_key(policy["interval"], datetime(2026, 9, 25, tzinfo=timezone.utc))
    pname = partition_manager.partition_name(policy["table"], key)
    assert pname == "audit_log_entries__2026-09"
    sql = partition_manager.build_partition_create_sql(policy, key)
    assert sql.startswith("CREATE TABLE IF NOT EXISTS audit_log_entries__2026-09 (LIKE audit_log_entries")


class _FakeConn:
    def __init__(self, log):
        self.log = log

    async def execute(self, statement):
        self.log.append(str(statement))


class _FakeEngine:
    def __init__(self, log):
        self.log = log

    def begin(self):
        class _Ctx:
            def __init__(self, log):
                self.log = log

            async def __aenter__(self):
                return _FakeConn(self.log)

            async def __aexit__(self, *exc):
                return False

        return _Ctx(self.log)


@pytest.mark.asyncio
async def test_partition_manager_ensure_archive_drop_cycle():
    log: list[str] = []
    engine = _FakeEngine(log)
    manager = partition_manager.PartitionManager(
        policies=[
            {
                "policy_id": "audit_monthly",
                "table": "audit_log_entries",
                "interval": "monthly",
                "retention_days": 90,
                "enabled": True,
            }
        ]
    )

    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    created = await manager.ensure_partition(engine, manager.policies()[0], now)
    assert created["created"] is True
    assert created["partition"] == "audit_log_entries__2026-09"

    again = await manager.ensure_partition(engine, manager.policies()[0], now)
    assert again["created"] is False  # idempotent

    assert len(manager.list_partitions()) == 1

    archived = await manager.archive_partition(engine, "audit_log_entries", "2026-09")
    assert archived["archived"] is True
    assert manager.list_partitions() == []


@pytest.mark.asyncio
async def test_partition_manager_drops_retired_partitions_only():
    log: list[str] = []
    engine = _FakeEngine(log)
    manager = partition_manager.PartitionManager(
        policies=[
            {
                "policy_id": "audit_monthly",
                "table": "audit_log_entries",
                "interval": "monthly",
                "retention_days": 90,
                "enabled": True,
            }
        ]
    )
    old = datetime(2020, 1, 15, tzinfo=timezone.utc)
    await manager.ensure_partition(engine, manager.policies()[0], old)
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    dropped = await manager.drop_expired_partitions(engine, now)
    assert len(dropped) == 1
    assert dropped[0]["partition"] == "audit_log_entries__2020-01"
    assert manager.list_partitions() == []


@pytest.mark.asyncio
async def test_partition_run_cycle_ensures_current_and_cleans():
    log: list[str] = []
    engine = _FakeEngine(log)
    manager = partition_manager.PartitionManager(
        policies=[
            {
                "policy_id": "audit_monthly",
                "table": "audit_log_entries",
                "interval": "monthly",
                "retention_days": 90,
                "enabled": True,
            },
            {
                "policy_id": "security_monthly",
                "table": "security_events",
                "interval": "monthly",
                "retention_days": 180,
                "enabled": True,
            },
        ]
    )
    # Seed an expired partition from a retired policy.
    old_policy = dict(manager.policies()[0])
    await manager.ensure_partition(engine, old_policy, datetime(2019, 5, 1, tzinfo=timezone.utc))

    result = await manager.run_cycle(engine, datetime(2026, 9, 25, tzinfo=timezone.utc))
    assert len(result["ensured"]) == 2  # both enabled policies got current partitions
    assert len(result["dropped"]) == 1  # the retired one was expired
    assert len(manager.list_partitions()) == 2


def test_partition_manager_catalog_shape():
    catalog = partition_manager.build_partition_manager_catalog()
    assert catalog["intervals"] == ["daily", "weekly", "monthly"]
    assert "policies" in catalog and "active_partitions" in catalog
    assert "worker_enabled" in catalog and "cycle_seconds" in catalog


# --- tenant routing -----------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_router_registers_and_disposes_dedicated_engine():
    router = tenant_router.TenantRouter()
    engine = await router.register("acme", dsn="postgresql+asyncpg://u:p@localhost:5432/acme")
    assert router.snapshot()["acme"]["mode"] == "dedicated_engine"
    assert await router.register("acme") is engine  # idempotent
    assert (await router.engine_for("acme")) is engine
    assert await router.deregister("acme") is True
    assert "acme" not in router.snapshot()


@pytest.mark.asyncio
async def test_tenant_router_falls_back_to_shared_default():
    router = tenant_router.TenantRouter()
    engine = await router.engine_for("unknown-tenant")
    assert router.snapshot()["unknown-tenant"]["mode"] == "shared_default"
    await router.dispose_all()
    assert router.snapshot() == {}


def test_request_tenant_id_resolution_order():
    from starlette.requests import Request

    def make(headers=None, query=b""):
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": headers or [],
                "query_string": query,
                "server": ("testserver", 80),
            }
        )

    assert tenant_router.get_request_tenant_id(
        make(headers=[(b"x-tenant-id", b"acme")], query=b"tenant_id=other")
    ) == "acme"
    assert tenant_router.get_request_tenant_id(make(query=b"tenant_id=beta")) == "beta"
    assert tenant_router.get_request_tenant_id(make()) == tenant_router.DEFAULT_TENANT


def test_tenant_router_catalog_shape():
    catalog = tenant_router.build_tenant_router_catalog()
    assert set(catalog) == {
        "default_tenant",
        "mode",
        "dsn_template_configured",
        "declared_tenants",
        "registered",
        "pool",
        "lookup_order",
    }
    assert catalog["mode"] in {"shared_default", "routed"}


# --- metadata surfaces --------------------------------------------------------


def test_meta_endpoints_document_multi_tenancy():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)

    tenants = client.get("/meta/tenants")
    assert tenants.status_code == 200
    assert "mode" in tenants.json() and "registered" in tenants.json()

    partitions = client.get("/meta/partitions")
    assert partitions.status_code == 200
    assert partitions.json()["policies"]

    ecosystem = client.get("/meta/ecosystem").json()["subservices"]
    assert "tenant_routing" in ecosystem
    assert "partition_management" in ecosystem

    features = client.get("/meta/features").json()["endpoints"]
    assert features["tenant_routing"] == "/meta/tenants"
    assert features["partition_management"] == "/meta/partitions"

    scoring = client.get("/meta/scoring-catalog").json()
    assert "tenant_routing" in scoring
    assert "partition_lifecycle" in scoring
    assert scoring["tenant_routing"]["mode"] in {"shared_default", "routed"}