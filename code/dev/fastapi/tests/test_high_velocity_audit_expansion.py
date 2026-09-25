"""Tests for Stage 1C: high-velocity audit isolation.

Covers the immutable protobuf transaction format (``app/protobuf_transaction_spec.py``),
the async high-throughput pipeline (``app/high_throughput_pipeline.py``), and
the new audit router surfaces (``/audit/pipeline/*``, ``/audit/transactions*``).
"""
from datetime import datetime, timezone

import pytest

from app import deps, high_throughput_pipeline, protobuf_transaction_spec
from app.high_throughput_pipeline import HighThroughputPipeline, PipelineEvent, protobuf_transaction_sink


# --- protobuf wire format -----------------------------------------------------


def test_varint_round_trip():
    for value in [0, 1, 127, 128, 300, 2**32, 2**63 - 1]:
        encoded = protobuf_transaction_spec._encode_varint(value)
        decoded, offset = protobuf_transaction_spec._decode_varint(encoded)
        assert decoded == value
        assert offset == len(encoded)


def test_transaction_bytes_are_deterministic():
    tx_a = protobuf_transaction_spec.Transaction(
        tx_id="abc", cursor=1, action="auth.login",
        payload={"user": "alice", "n": 2}, occurred_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )
    tx_b = protobuf_transaction_spec.Transaction(
        tx_id="abc", cursor=1, action="auth.login",
        payload={"user": "alice", "n": 2}, occurred_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )
    assert tx_a.to_bytes() == tx_b.to_bytes()
    assert tx_a.sha256_hex() == tx_b.sha256_hex()


def test_transaction_bytes_round_trip():
    original = protobuf_transaction_spec.Transaction(
        tx_id="tx-7", cursor=3, action="risk.evaluate",
        entity_type="user", entity_id="42",
        actor_user_id=9, tenant_id="acme",
        payload={"score": 40, "severity": "warning"},
        prev_hash=bytes(range(32)),
        occurred_at=datetime(2026, 9, 25, 12, 30, 0, tzinfo=timezone.utc),
    )
    decoded = protobuf_transaction_spec.Transaction.from_bytes(original.to_bytes())
    assert decoded.tx_id == original.tx_id
    assert decoded.cursor == original.cursor
    assert decoded.action == original.action
    assert decoded.actor_user_id == 9
    assert decoded.tenant_id == "acme"
    assert decoded.payload == original.payload
    assert decoded.prev_hash == original.prev_hash
    assert decoded.occurred_at == original.occurred_at


def test_transaction_sign_and_verify():
    tx = protobuf_transaction_spec.Transaction(tx_id="t1", cursor=1, action="auth.login")
    signed = protobuf_transaction_spec.sign_transaction(tx)
    assert signed.signature
    # unsigned frames are permitted; signed frames must verify.
    assert protobuf_transaction_spec.verify_transaction_signature(tx)
    assert protobuf_transaction_spec.verify_transaction_signature(signed)


def test_transaction_log_chains_hashes_and_verifies():
    log = protobuf_transaction_spec.TransactionLog()
    first = log.append(action="auth.login", tx_id="t1", payload={"user": "alice"})
    second = log.append(action="risk.evaluate", tx_id="t2", payload={"score": 40})
    assert first.prev_hash == b""
    assert second.prev_hash == first.digest()
    assert log.total() == 2
    assert log.verify_chain()["valid"] is True
    tail = log.tail(5)
    assert tail[0]["tx_id"] == "t1" and tail[0]["signature_present"] is False


def test_transaction_log_detects_tampered_frame():
    import dataclasses

    log = protobuf_transaction_spec.TransactionLog()
    log.append(action="auth.login", tx_id="t1")
    log.append(action="risk.evaluate", tx_id="t2")
    # Simulate tampering: break the second frame's chain pointer.
    log._entries[1] = dataclasses.replace(log._entries[1], prev_hash=b"\x00" * 32)
    verdict = log.verify_chain()
    assert verdict["valid"] is False
    assert verdict["broken_at"] == 1


def test_transaction_spec_catalog_exposes_schema():
    catalog = protobuf_transaction_spec.build_transaction_spec_catalog()
    fields = {entry["name"]: entry["field"] for entry in catalog["fields"]}
    assert fields["prev_hash"] == 11 and fields["signature"] == 12
    assert catalog["hash_chain"]["algo"] == "sha256"
    assert "append-only" in catalog["hash_chain"]["policy"]


# --- high-throughput pipeline -------------------------------------------------


async def _capture_sink(log):
    async def sink(batch):
        log.extend(batch)

    return sink


@pytest.mark.asyncio
async def test_pipeline_batches_to_sink_via_drain():
    captured: list[PipelineEvent] = []
    pipeline = HighThroughputPipeline(workers=1, batch_size=10, max_queue=100, sink=await _capture_sink(captured))
    event = pipeline.submit_event(kind="auth.login", severity="warning", entity_type="user", entity_id="7")
    assert event is not None
    pending = pipeline.stats()["pending"]
    assert pending == 1  # accepted but not yet drained (no workers running)
    await pipeline.drain()
    assert len(captured) == 1
    assert captured[0].kind == "auth.login"
    stats = pipeline.stats()
    assert stats["processed"] == 1 and stats["batches"] == 1 and stats["pending"] == 0


@pytest.mark.asyncio
async def test_pipeline_rejects_when_queue_full():
    captured: list[PipelineEvent] = []
    pipeline = HighThroughputPipeline(max_queue=2, sink=await _capture_sink(captured))
    assert pipeline.submit_event(kind="a.one") is not None
    assert pipeline.submit_event(kind="a.two") is not None
    assert pipeline.submit_event(kind="a.three") is None  # queue full -> backpressure
    assert pipeline.stats()["rejected"] == 1
    await pipeline.drain()
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_pipeline_dead_letters_failed_batches():
    async def failing_sink(batch):
        raise RuntimeError("sink down")

    pipeline = HighThroughputPipeline(max_queue=10, sink=failing_sink)
    pipeline.submit_event(kind="auth.login")
    await pipeline.drain()
    stats = pipeline.stats()
    assert stats["processed"] == 1 and stats["dead_lettered"] == 1
    report = pipeline.dead_letter_report()
    assert report["dead_lettered"] == 1
    assert "sink down" in report["recent"][0]["error"]


@pytest.mark.asyncio
async def test_pipeline_default_sink_writes_immutable_transactions():
    log = protobuf_transaction_spec.TransactionLog()
    pipeline = HighThroughputPipeline(sink=protobuf_transaction_sink(log))
    pipeline.submit_event(kind="auth.login.risk", severity="warning", tenant_id="acme", payload={"score": 40})
    pipeline.submit_event(kind="access.denied", severity="critical", payload={"cell": "phone"})
    await pipeline.drain()

    assert log.total() == 2
    assert log.verify_chain()["valid"] is True
    tail = log.tail(2)
    assert tail[0]["action"] == "auth.login.risk"
    assert tail[0]["tenant_id"] == "acme"
    assert tail[0]["payload"]["severity"] == "warning"
    assert tail[1]["cursor"] == 2


def test_pipeline_catalog_shape():
    catalog = high_throughput_pipeline.build_pipeline_catalog()
    assert set(catalog["tunables"]) == {"workers", "batch_size", "max_queue", "flush_seconds"}
    assert catalog["default_sink"] == "protobuf_transaction_sink (immutable hash-chained frames)"


# --- endpoint wiring ----------------------------------------------------------


class _FakeAdmin:
    id = 1
    username = "admin"
    is_admin = True


class _RestorePoints:
    pass


def _install_defaults(pipeline=None, log=None):
    """Swap module singletons for deterministic endpoint tests; return restore fn."""
    from app import high_throughput_pipeline as htp
    from app import protobuf_transaction_spec as pts

    prev_pipe, prev_log = htp.get_default_pipeline(), pts.get_default_transaction_log()
    if pipeline is not None:
        htp.set_default_pipeline(pipeline)
    if log is not None:
        pts.set_default_transaction_log(log)

    def restore():
        htp.set_default_pipeline(prev_pipe)
        pts.set_default_transaction_log(prev_log)

    return restore


def _admin_override(app):
    async def _admin():
        return _FakeAdmin()

    app.dependency_overrides[deps.get_current_admin_user] = _admin


def test_pipeline_event_endpoint_accepts_and_stats_report():
    from fastapi.testclient import TestClient

    from app.main import app

    restore = _install_defaults(pipeline=HighThroughputPipeline(max_queue=10))
    _admin_override(app)
    try:
        client = TestClient(app)
        response = client.post(
            "/audit/pipeline/event",
            json={
                "kind": "risk.evaluate",
                "severity": "warning",
                "entity_type": "user",
                "entity_id": "7",
                "actor_user_id": 1,
                "tenant_id": "acme",
                "payload": {"score": 42},
            },
        )
        assert response.status_code == 202
        payload = response.json()
        assert payload["accepted"] is True
        assert payload["event_id"]

        stats = client.get("/audit/pipeline/stats")
        assert stats.status_code == 200
        body = stats.json()
        assert body["submitted"] == 1 and body["pending"] == 1
        assert body["workers"] == high_throughput_pipeline.PIPELINE_WORKERS
    finally:
        restore()
        app.dependency_overrides.pop(deps.get_current_admin_user, None)


def test_pipeline_endpoints_require_admin():
    from fastapi.testclient import TestClient

    from app.main import app

    restore = _install_defaults(pipeline=HighThroughputPipeline())
    try:
        class _FakeNonAdmin:
            id = 2
            username = "customer"
            is_admin = False

        async def _non_admin():
            return _FakeNonAdmin()

        # Override only get_current_user -> the real admin check must reject.
        app.dependency_overrides[deps.get_current_user] = _non_admin
        try:
            response = TestClient(app).post(
                "/audit/pipeline/event", json={"kind": "auth.login", "summary": "nope"}
            )
            assert response.status_code == 403
        finally:
            app.dependency_overrides.pop(deps.get_current_user, None)
    finally:
        restore()


def test_pipeline_event_rejects_invalid_severity():
    from fastapi.testclient import TestClient

    from app.main import app

    restore = _install_defaults(pipeline=HighThroughputPipeline())
    _admin_override(app)
    try:
        response = TestClient(app).post(
            "/audit/pipeline/event", json={"kind": "auth.login", "severity": "loud"}
        )
        assert response.status_code == 422
    finally:
        restore()
        app.dependency_overrides.pop(deps.get_current_admin_user, None)


def test_transactions_endpoints_report_immutable_log():
    from fastapi.testclient import TestClient

    from app.main import app

    log = protobuf_transaction_spec.TransactionLog()
    log.append(action="auth.login", tx_id="t1", payload={"user": "alice"})
    restore = _install_defaults(log=log)
    _admin_override(app)
    try:
        client = TestClient(app)
        tx = client.get("/audit/transactions?limit=5")
        assert tx.status_code == 200
        body = tx.json()
        assert body["total"] == 1
        assert body["chain"]["valid"] is True
        assert body["tail"][0]["tx_id"] == "t1"

        spec = client.get("/audit/transactions/spec")
        assert spec.status_code == 200
        spec_body = spec.json()["spec"]
        assert spec_body["version"] == 1
        assert any(f["name"] == "prev_hash" for f in spec_body["fields"])
    finally:
        restore()
        app.dependency_overrides.pop(deps.get_current_admin_user, None)


def test_meta_documents_high_velocity_audit():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    ecosystem = client.get("/meta/ecosystem").json()["subservices"]
    assert "high_velocity_audit" in ecosystem
    assert ecosystem["high_velocity_audit"]["status"] == "ready"

    features = client.get("/meta/features").json()["endpoints"]
    assert features["pipeline_event"] == "/audit/pipeline/event"
    assert features["transactions"] == "/audit/transactions"

    scoring = client.get("/meta/scoring-catalog").json()["event_pipeline"]
    assert set(scoring) == {"pipeline", "protobuf_transaction_spec"}
    assert scoring["protobuf_transaction_spec"]["encoding"].startswith("protobuf")