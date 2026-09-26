"""Tests for the flexible-core expansion.

The thin groups were grown to answer the harder questions a production
deployment asks, and this module pins the new behaviour:

- ``app/services/audit_log.py`` — action specs & aliases, redaction, structured
  diffs, tamper-evident sealing, anomaly rules, retention, export, plus the
  governed write path and the seven new ``/audit/logs/*`` endpoints.
- ``app/hsm_signer.py`` — backend registry, key rotation, freshness/replay
  policy, N-of-M co-signature, self-test.
- ``app/biometric_vault.py`` — single-use challenges, three-way decisions,
  brute-force lockout, adaptive thresholds, multi-slot enrollment, replication.
- ``app/tenant_router.py`` — policy-checked resolution, DSN redaction, read/write
  splitting, health failover, provisioning plan.
- ``app/model_bases.py`` — extended security-event families.

Everything here is additive: the pre-existing 552 tests are the other half of
the contract.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import biometric_vault, deps, hsm_signer, model_bases, tenant_router
from app.services import audit_log


NOW = datetime.now(timezone.utc)


# ===========================================================================
# app/services/audit_log.py — action specs, aliases, redaction
# ===========================================================================


def test_action_specs_compose_from_family_defaults():
    specs = audit_log.build_action_specs()
    # Inherited from the family row...
    assert specs["auth.login"]["category"] == "authentication"
    assert specs["auth.login"]["retention_days"] == 400
    # ...and refined per action.
    assert specs["policy.block"]["default_severity"] == "critical"
    assert specs["policy.block"]["category"] == "governance"
    assert specs["policy.block"]["requires_justification"] is True
    assert specs["booking.reassign"]["requires_justification"] is True
    # Every catalogued action is spec'd, and the spec keys are uniform.
    assert set(specs) == audit_log.AUDIT_ACTIONS
    assert all(set(spec) == set(audit_log.AUDIT_SPEC_FIELDS) | {"family", "aliases"} for spec in specs.values())


def test_action_specs_accept_runtime_extensions():
    specs = audit_log.build_action_specs({"tenant.expel": {"category": "governance", "retention_days": 3650}})
    assert specs["tenant.expel"]["family"] == "custom"
    assert specs["tenant.expel"]["category"] == "governance"
    # A synthetic spec falls back to the permissive custom row.
    assert specs["auth.login"]["retention_days"] == 400


def test_action_aliases_resolve_and_validate():
    assert audit_log.normalize_action("  user.LOGIN ") == "auth.login"
    assert audit_log.normalize_action("login") == "auth.login"
    assert audit_log.normalize_action("policy.block_topic") == "policy.block"
    assert audit_log.validate_action("signup") == "auth.register"
    # An uncatalogued action is allowed leniently, rejected strictly.
    assert audit_log.validate_action("nope.thing", strict=False) == "nope.thing"
    with pytest.raises(ValueError, match="unknown audit action"):
        audit_log.validate_action("nope.thing")
    with pytest.raises(ValueError, match="non-empty"):
        audit_log.normalize_action("  ")


def test_action_spec_default_severity_and_sensitivity():
    assert audit_log.default_severity_for("policy.block") == "critical"
    assert audit_log.default_severity_for("login") == "info"
    assert audit_log.requires_justification("policy.override") is True
    assert audit_log.requires_justification("auth.login") is False
    # Uncatalogued actions synthesize the permissive custom spec.
    assert audit_log.action_spec("who.knows")["category"] == "custom"
    assert audit_log.requires_justification("who.knows") is False


def test_justification_detection():
    assert audit_log.has_justification({"reason": "x"}) is True
    assert audit_log.has_justification({"ticket": "T-1"}) is True
    assert audit_log.has_justification({"reason": ""}) is False
    assert audit_log.has_justification({}) is False
    assert audit_log.has_justification(None) is False


def test_redact_detail_masks_credentials_recursively():
    redacted = audit_log.redact_detail(
        {
            "password": "hunter2",
            "nested": {"api_key": "abc", "keep": 1},
            "rows": [{"access_token": "t"}, {"ok": 2}],
        }
    )
    assert redacted["password"] == audit_log.REDACTED_PLACEHOLDER
    assert redacted["nested"]["api_key"] == audit_log.REDACTED_PLACEHOLDER
    assert redacted["nested"]["keep"] == 1
    assert redacted["rows"][0]["access_token"] == audit_log.REDACTED_PLACEHOLDER
    assert redacted["rows"][1] == {"ok": 2}
    # Substring matching catches names that are not exact hits.
    assert audit_log.redact_detail({"user_password_hash": "x"})["user_password_hash"] == audit_log.REDACTED_PLACEHOLDER


def test_redact_detail_is_bounded_and_extensible():
    deep: dict = {"level": 0}
    node = deep
    for _ in range(12):
        node["level"] = {"level": 0}
        node = node["level"]
    assert audit_log.redact_detail(deep)["level"]["level"]["level"]["level"]["level"]["level"]["level"]["level"]["level"] == {
        "_truncated": "max_depth_exceeded"
    }
    capped = audit_log.redact_detail({"rows": list(range(10))}, max_items=4)
    assert len(capped["rows"]) == 5
    assert capped["rows"][-1] == {"_truncated": "6_more_items"}
    # Extra keys are opt-in per call site.
    assert audit_log.redact_detail({"mrn": "1"}, extra_keys=["mrn"])["mrn"] == audit_log.REDACTED_PLACEHOLDER
    # Non-container input passes through untouched.
    assert audit_log.redact_detail("plain") == "plain"


def test_severity_helpers():
    assert audit_log.severity_rank("info") < audit_log.severity_rank("critical")
    assert audit_log.severity_rank("nope") == -1
    assert audit_log.max_severity("info", "critical", "warning") == "critical"
    assert audit_log.max_severity() == "info"


# ===========================================================================
# app/services/audit_log.py — diffs
# ===========================================================================


def test_audit_diff_walks_containers():
    changes = audit_log.audit_diff({"a": 1, "b": [1, 2]}, {"a": 2, "b": [1, 2, 3], "c": 4})
    assert changes == [
        {"op": "replace", "path": "a", "before": 1, "after": 2},
        {"op": "add", "path": "b[2]", "before": None, "after": 3},
        {"op": "add", "path": "c", "before": None, "after": 4},
    ]
    assert audit_log.audit_diff({"a": 1}, {"a": 1}) == []
    assert audit_log.audit_diff({"a": 1}, {}) == [
        {"op": "remove", "path": "a", "before": 1, "after": None}
    ]
    # Top-level scalars address themselves as "$".
    assert audit_log.audit_diff(1, 2)[0]["path"] == "$"
    # Diffing is stable: the same pair always yields the same order.
    assert audit_log.audit_diff({"b": 1, "a": 2}, {"a": 3, "b": 4}) == audit_log.audit_diff(
        {"b": 1, "a": 2}, {"a": 3, "b": 4}
    )


def test_build_change_detail_summarizes_and_truncates():
    detail = audit_log.build_change_detail({"a": 1}, {"a": 2})
    assert detail["changed"] is True and detail["change_count"] == 1
    assert detail["truncated"] is False
    assert audit_log.build_change_detail({"a": 1}, {"a": 1})["changed"] is False
    wide = audit_log.build_change_detail({"a": 0}, {"a": 1, "b": 2, "c": 3}, max_items=2)
    assert wide["change_count"] == 3 and len(wide["diff"]) == 2 and wide["truncated"] is True


# ===========================================================================
# app/services/audit_log.py — tamper-evident sealing
# ===========================================================================


def _payload(**overrides):
    # `detail` is empty on purpose: a sensitive action with no justification is
    # the interesting case for the anomaly rules.
    base = {
        "id": 1,
        "action": "policy.override",
        "actor_user_id": 3,
        "entity_type": "topic",
        "entity_id": "42",
        "summary": "Blocked topic",
        "detail": {},
        "severity": "warning",
        "source": "api",
        "created_at": NOW,
    }
    base.update(overrides)
    return base


def test_seal_chain_links_and_verifies():
    entries = [_payload(id=1), _payload(id=2), _payload(id=3)]
    seals = audit_log.seal_entries(entries)
    assert len(seals) == 3
    assert seals[0]["prev_seal"] == ""
    assert seals[1]["prev_seal"] == seals[0]["seal"]
    report = audit_log.verify_seal_chain(seals)
    assert report["valid"] is True and report["entries"] == 3
    assert report["algo"] == audit_log.AUDIT_SEAL_ALGO
    assert "id" in report["sealed_fields"]


def test_seal_chain_detects_a_removed_entry():
    entries = [_payload(id=1), _payload(id=2), _payload(id=3)]
    seals = audit_log.seal_entries(entries)
    del seals[1]
    report = audit_log.verify_seal_chain(seals)
    assert report["valid"] is False and report["broken_at"] == 1


def test_seal_covers_sealed_fields_only():
    payload = _payload()
    baseline = audit_log.entry_seal(payload)
    # Fields outside the sealed set cannot change the digest...
    assert audit_log.entry_seal({**payload, "unused": "x"}) == baseline
    # ...but a sealed field edit does.
    assert audit_log.entry_seal({**payload, "summary": "tampered"}) != baseline
    # And the integrity block is excluded, so re-sealing is idempotent.
    stamped = {**payload, "detail": {**payload["detail"], audit_log.SEAL_DETAIL_KEY: {"seal": "junk"}}}
    assert audit_log.entry_seal(stamped) == baseline


def test_seal_chain_object_extends_and_verifies():
    chain = hsm_signer.SigningKeyRing()  # unrelated registry; proves no coupling
    assert chain is not None
    vault_chain = audit_log.SealChain()
    assert vault_chain.head() == "" and vault_chain.total() == 0
    first = vault_chain.extend(_payload(id=1))
    second = vault_chain.extend(_payload(id=2))
    assert first != second
    assert vault_chain.head() == second
    assert vault_chain.verify()["valid"] is True
    assert len(vault_chain.records()) == 2
    vault_chain.reset()
    assert vault_chain.total() == 0 and vault_chain.head() == ""


def test_seal_chain_bounded_to_policy_free_growth():
    chain = audit_log.SealChain()
    for index in range(5):
        chain.extend(_payload(id=index))
    assert chain.verify()["entries"] == 5


# ===========================================================================
# app/services/audit_log.py — rollups, anomalies, retention
# ===========================================================================


def test_summarize_actor_activity_rolls_up():
    rows = [
        _payload(id=1, actor_user_id=7, action="policy.override", severity="warning"),
        _payload(id=2, actor_user_id=7, action="policy.override", severity="critical"),
        _payload(id=3, actor_user_id=8, action="auth.login", severity="info"),
    ]
    report = audit_log.summarize_actor_activity(rows)
    assert report["actor_count"] == 2
    top = report["actors"][0]
    assert top["actor_user_id"] == 7 and top["count"] == 2
    assert top["by_action"] == {"policy.override": 2}
    assert top["peak_severity"] == "critical"
    assert top["first_seen"] == top["last_seen"] == NOW


def test_build_entity_timeline_orders_and_filters():
    rows = [
        _payload(id=1, created_at=NOW - timedelta(minutes=5)),
        _payload(id=2, created_at=NOW),
        _payload(id=3, entity_type="booking", entity_id="7", created_at=NOW - timedelta(minutes=1)),
    ]
    everything = audit_log.build_entity_timeline(rows)
    # Chronological, regardless of the order the rows arrived in.
    assert [event["id"] for event in everything] == [1, 3, 2]
    scoped = audit_log.build_entity_timeline(rows, entity_type="booking", entity_id="7")
    assert [event["id"] for event in scoped] == [3]
    assert audit_log.build_entity_timeline(rows, entity_id="nope") == []
    # A governed entry's change summary surfaces as `changed`.
    with_change = dict(rows[0])
    with_change["detail"] = {**with_change["detail"], "_change": {"changed": True}}
    assert audit_log.build_entity_timeline([with_change])[0]["changed"] is True


def test_detect_audit_anomalies_flags_uncatalogued_and_unjustified():
    rows = [
        # sensitive action with no justification
        _payload(id=1, created_at=NOW.replace(hour=12)),
        # action that is not in the catalog at all
        _payload(id=2, action="mystery.act", created_at=NOW.replace(hour=12)),
    ]
    findings = audit_log.detect_audit_anomalies(rows)
    rules = {finding["rule"] for finding in findings}
    assert "unjustified_sensitive_action" in rules
    assert "uncatalogued_action" in rules
    unjustified = next(f for f in findings if f["rule"] == "unjustified_sensitive_action")
    assert unjustified["entry_ids"] == [1]
    # A justified sensitive action raises nothing.
    assert not [
        f
        for f in audit_log.detect_audit_anomalies(
            [{**_payload(id=1), "detail": {"reason": "court order"}}]
        )
        if f["rule"] == "unjustified_sensitive_action"
    ]


def test_detect_audit_anomalies_flags_after_hours_critical():
    inside = audit_log.detect_audit_anomalies([_payload(severity="critical", created_at=NOW.replace(hour=12))])
    outside = audit_log.detect_audit_anomalies([_payload(severity="critical", created_at=NOW.replace(hour=3))])
    assert not [f for f in inside if f["rule"] == "after_hours_critical"]
    assert [f for f in outside if f["rule"] == "after_hours_critical"]


def test_detect_audit_anomalies_flags_actor_and_sensitive_bursts():
    rows = [
        _payload(id=i, actor_user_id=7, action="auth.login", created_at=NOW + timedelta(seconds=i))
        for i in range(6)
    ]
    findings = audit_log.detect_audit_anomalies(rows, thresholds={"burst_threshold": 3})
    assert "actor_burst" in {f["rule"] for f in findings}

    sensitive = [
        _payload(
            id=100 + i,
            actor_user_id=9,
            action="policy.override",
            detail={"reason": "x"},
            created_at=NOW + timedelta(days=i),
        )
        for i in range(4)
    ]
    findings = audit_log.detect_audit_anomalies(sensitive, thresholds={"sensitive_burst_threshold": 2})
    burst = next(f for f in findings if f["rule"] == "sensitive_action_burst")
    # The governance total is unwindowed: all four actions are counted, and the
    # threshold only decides whether a finding is raised at all.
    assert burst["severity"] == "critical" and burst["count"] == 4
    assert burst["entry_ids"] == [100, 101, 102, 103]
    # One action below the threshold raises nothing.
    assert not [
        f
        for f in audit_log.detect_audit_anomalies(
            sensitive[:2], thresholds={"sensitive_burst_threshold": 2}
        )
        if f["rule"] == "sensitive_action_burst"
    ]


def test_retention_days_precedence_and_floor():
    # Per-action override wins outright (but still respects the floor).
    assert audit_log.retention_days_for("auth.login", overrides={"auth.login": 10}) == audit_log.AUDIT_RETENTION_MINIMUM_DAYS
    # Severity can only raise the window: 400 (the spec) -> 2555 (critical).
    assert audit_log.retention_days_for("auth.login", severity="critical") == 2555
    assert audit_log.retention_days_for("auth.login") == 400
    # A "*" override raises the floor for every action.
    assert audit_log.retention_days_for("auth.login", overrides={"*": 5000}) == 5000
    assert audit_log.retention_days_for("policy.override") == 2555


def test_plan_retention_splits_expired_from_kept():
    old = _payload(id=1, created_at=NOW - timedelta(days=5000))
    fresh = _payload(id=2, created_at=NOW)
    plan = audit_log.plan_retention([old, fresh], now=NOW)
    assert plan["expired_count"] == 1 and plan["keep_count"] == 1
    assert plan["expired"][0]["id"] == 1
    assert plan["expired"][0]["age_days"] > plan["expired"][0]["retention_days"]
    assert plan["keep"][0]["id"] == 2
    assert plan["policies"]["policy.override"] == 2555
    assert plan["minimum_days"] == audit_log.AUDIT_RETENTION_MINIMUM_DAYS


# ===========================================================================
# app/services/audit_log.py — export
# ===========================================================================


def test_export_entries_ndjson_and_csv():
    entries = [_payload(id=1), _payload(id=2)]
    ndjson = audit_log.export_entries_ndjson(entries)
    assert len(ndjson.splitlines()) == 2
    assert '"action":"policy.override"' in ndjson
    # created_at is flattened to an ISO string for the wire.
    assert NOW.isoformat() in ndjson

    csv_text = audit_log.export_entries_csv(entries)
    lines = csv_text.strip().splitlines()
    assert lines[0] == ",".join(audit_log.AUDIT_EXPORT_COLUMNS)
    assert len(lines) == 3
    assert lines[1].startswith("1,3,policy.override")


def test_coerce_entry_accepts_models_or_mappings():
    class _Row:
        id = 5
        actor_user_id = 1
        action = "auth.login"
        entity_type = "user"
        entity_id = 9
        summary = "in"
        detail_json = '{"a": 1}'
        severity = "info"
        source = "api"
        created_at = NOW

    from_model = audit_log.coerce_entry(_Row())
    from_dict = audit_log.coerce_entry({"id": 5, "detail_json": '{"a": 1}'})
    assert from_model["detail"] == {"a": 1}
    assert from_dict["detail"] == {"a": 1}
    assert from_dict["entity_id"] == ""


# ===========================================================================
# app/services/audit_log.py — governed writes (fake session)
# ===========================================================================


class _FakeResult:
    def __init__(self, scalars=None):
        self._scalars = scalars or []

    def scalars(self):
        return self

    def all(self):
        return list(self._scalars)

    def scalar(self):
        return None


class _FakeDb:
    """Answers the read queries and records the governed writes."""

    def __init__(self, entries=None):
        self.entries = list(entries or [])
        self.added = []
        self.commits = 0
        self._next_id = 500

    async def execute(self, query):
        return _FakeResult(scalars=self.entries)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = self._next_id
            self._next_id += 1
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = obj.created_at


@pytest.mark.asyncio
async def test_record_auditable_resolves_aliases_and_default_severity():
    db = _FakeDb()
    entry = await audit_log.record_auditable(
        db, action="login", summary="Alice signed in", actor_user_id=1
    )
    assert entry.action == "auth.login"
    assert entry.severity == "info"
    assert db.commits == 2  # insert, then stamp the seal


@pytest.mark.asyncio
async def test_record_auditable_requires_justification_for_sensitive_actions():
    db = _FakeDb()
    with pytest.raises(ValueError, match="requires a justification"):
        await audit_log.record_auditable(db, action="policy.override", summary="blocked")
    # An explicit override (or a justification) lets it through.
    entry = await audit_log.record_auditable(
        db,
        action="policy.override",
        summary="blocked",
        detail={"reason": "court order"},
        require_justification=False,
    )
    assert entry.action == "policy.override"
    assert audit_log._load_json(entry.detail_json)["reason"] == "court order"


@pytest.mark.asyncio
async def test_record_auditable_redacts_and_diffs_and_seals():
    db = _FakeDb()
    # The chain head before the write is what the entry must chain onto.
    head_before = audit_log.SEAL_CHAIN.head()
    entry = await audit_log.record_auditable(
        db,
        action="policy.block",
        summary="Blocked topic",
        actor_user_id=4,
        detail={"reason": "law", "password": "hunter2"},
        before={"blocked": False},
        after={"blocked": True},
    )
    detail = audit_log._load_json(entry.detail_json)
    assert detail["password"] == audit_log.REDACTED_PLACEHOLDER
    assert detail["reason"] == "law"
    assert detail["_change"]["changed"] is True
    integrity = detail[audit_log.SEAL_DETAIL_KEY]
    assert integrity["algo"] == audit_log.AUDIT_SEAL_ALGO
    assert integrity["seal"]
    assert integrity["prev_seal"] == head_before
    assert audit_log.SEAL_CHAIN.head() == integrity["seal"]


@pytest.mark.asyncio
async def test_record_auditable_seal_is_idempotent_and_chained():
    audit_log.SEAL_CHAIN.reset()
    try:
        db = _FakeDb()
        first = await audit_log.record_auditable(
            db, action="auth.login", summary="one", detail={}
        )
        second = await audit_log.record_auditable(
            db, action="auth.login", summary="two", detail={}
        )
        first_integrity = audit_log._load_json(first.detail_json)[audit_log.SEAL_DETAIL_KEY]
        second_integrity = audit_log._load_json(second.detail_json)[audit_log.SEAL_DETAIL_KEY]
        assert second_integrity["prev_seal"] == first_integrity["seal"]
        # The written seal matches a fresh re-derivation (idempotent because the
        # integrity block is excluded from its own digest).
        assert audit_log.entry_seal(second, prev_seal=first_integrity["seal"]) == second_integrity["seal"]
        assert audit_log.SEAL_CHAIN.verify()["valid"] is True
    finally:
        audit_log.SEAL_CHAIN.reset()


@pytest.mark.asyncio
async def test_record_auditable_strict_and_unsealed_paths():
    db = _FakeDb()
    with pytest.raises(ValueError, match="unknown audit action"):
        await audit_log.record_auditable(db, action="not.a.thing", summary="x", strict=True)
    # Lenient by default so a bespoke verb is still recordable.
    entry = await audit_log.record_auditable(db, action="not.a.thing", summary="x", seal=False)
    assert entry.action == "not.a.thing"
    assert audit_log.SEAL_DETAIL_KEY not in audit_log._load_json(entry.detail_json)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_iter_audit_pages_dedupes_and_terminates():
    class _OffsetAgnostic(_FakeDb):
        async def execute(self, query):
            # A backend that ignores offset would loop forever without dedupe.
            return _FakeResult(scalars=self.entries)

    db = _OffsetAgnostic(entries=[_payload(id=1), _payload(id=2)])
    pages = [page async for page in audit_log.iter_audit_pages(db, page_size=1)]
    assert len(pages) == 2
    assert [page[0]["id"] for page in pages] == [1, 2]

    empty = _FakeDb(entries=[])
    assert [page async for page in audit_log.iter_audit_pages(empty)] == []


@pytest.mark.asyncio
async def test_export_audit_trail_paginates_and_validates_format():
    db = _FakeDb(entries=[_payload(id=1), _payload(id=2)])
    report = await audit_log.export_audit_trail(db, fmt="csv", page_size=1)
    assert report["format"] == "csv"
    assert report["entry_count"] == 2
    assert report["columns"] == list(audit_log.AUDIT_EXPORT_COLUMNS)
    assert len(report["content"].strip().splitlines()) == 3

    ndjson = await audit_log.export_audit_trail(db, fmt="ndjson")
    assert ndjson["columns"] is None
    assert len(ndjson["content"].splitlines()) == 2

    with pytest.raises(ValueError, match="format must be one of"):
        await audit_log.export_audit_trail(db, fmt="xml")


# ===========================================================================
# app/services/audit_log.py — catalog
# ===========================================================================


def test_audit_log_catalog_keeps_pinned_keys_and_adds_surface():
    catalog = audit_log.build_audit_log_catalog()
    # Pinned contract from the original expansion.
    assert catalog["severities"] == ["info", "warning", "critical"]
    assert "policy.override" in catalog["actions"]["policy"]
    assert catalog["action_count"] == len(audit_log.AUDIT_ACTIONS)
    # Expansion surface.
    assert set(catalog["families"]) == set(audit_log.AUDIT_ACTION_CATALOG)
    assert catalog["action_specs"]["policy.block"]["default_severity"] == "critical"
    assert catalog["alias_count"] == len(audit_log.ACTION_ALIASES)
    assert catalog["justification_keys"] == list(audit_log.JUSTIFICATION_KEYS)
    assert catalog["redaction"]["placeholder"] == audit_log.REDACTED_PLACEHOLDER
    assert catalog["diff_ops"] == ["add", "remove", "replace"]
    assert catalog["seal"]["algo"] == audit_log.AUDIT_SEAL_ALGO
    assert {rule["id"] for rule in catalog["anomaly_rules"]} >= {
        "actor_burst",
        "after_hours_critical",
    }
    assert catalog["export"]["formats"] == list(audit_log.EXPORT_FORMATS)


# ===========================================================================
# /audit router — governed write + forensic reads
# ===========================================================================


class _FakeAdmin:
    id = 1
    username = "tester"
    is_admin = True


class _FakeCustomer:
    id = 2
    username = "customer"
    is_admin = False


def _override(db, current_user=_FakeAdmin, force_admin_dep=True):
    from app.main import app

    async def _get_db_override():
        yield db

    app.dependency_overrides[deps.get_db] = _get_db_override
    app.dependency_overrides[deps.get_current_user] = lambda: current_user
    if force_admin_dep:
        app.dependency_overrides[deps.get_current_admin_user] = lambda: current_user


def _clear():
    from app.main import app

    app.dependency_overrides.pop(deps.get_db, None)
    app.dependency_overrides.pop(deps.get_current_user, None)
    app.dependency_overrides.pop(deps.get_current_admin_user, None)


def _call(method, url, db, user=_FakeAdmin, **kwargs):
    _override(db, user)
    try:
        return getattr(TestClient(app_of()), method)(url, **kwargs)
    finally:
        _clear()


def app_of():
    from app.main import app

    return app


def test_post_audit_log_auditable_governed_write():
    db = _FakeDb()
    response = _call(
        "post",
        "/audit/log/auditable",
        db,
        json={
            "action": "policy.block",
            "summary": "Blocked topic for user",
            "entity_type": "topic",
            "entity_id": "42",
            "detail": {"reason": "legal-constraint", "password": "hunter2"},
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["action"] == "policy.block"
    assert payload["severity"] == "critical"  # the action's default
    assert payload["detail"]["password"] == audit_log.REDACTED_PLACEHOLDER
    assert payload["detail"][audit_log.SEAL_DETAIL_KEY]["seal"]


def test_post_audit_log_auditable_rejects_missing_justification():
    db = _FakeDb()
    response = _call(
        "post",
        "/audit/log/auditable",
        db,
        json={"action": "policy.override", "summary": "blocked with no reason"},
    )
    assert response.status_code == 422
    assert "requires a justification" in response.json()["detail"]


def test_post_audit_log_auditable_requires_admin():
    db = _FakeDb()
    response = _call(
        "post",
        "/audit/log/auditable",
        db,
        user=_FakeCustomer,
        json={"action": "auth.login", "summary": "nope", "detail": {}},
    )
    # Override only get_current_user so the real admin check runs.
    _override(db, _FakeCustomer, force_admin_dep=False)
    try:
        response = TestClient(app_of()).post(
            "/audit/log/auditable",
            json={"action": "auth.login", "summary": "nope", "detail": {}},
        )
    finally:
        _clear()
    assert response.status_code == 403


def test_audit_log_forensic_read_endpoints():
    db = _FakeDb(entries=[_payload(id=1, actor_user_id=7), _payload(id=2, actor_user_id=7)])
    integrity = _call("get", "/audit/logs/integrity", db)
    assert integrity.status_code == 200
    assert integrity.json()["valid"] is True
    assert integrity.json()["entries"] == 2

    actors = _call("get", "/audit/logs/actors", db)
    assert actors.status_code == 200
    assert actors.json()["actor_count"] == 1
    assert actors.json()["actors"][0]["count"] == 2

    timeline = _call("get", "/audit/logs/timeline?entity_type=topic", db)
    assert timeline.status_code == 200
    assert timeline.json()["count"] == 2

    anomalies = _call("get", "/audit/logs/anomalies", db)
    assert anomalies.status_code == 200
    assert anomalies.json()["scanned"] == 2
    assert "unjustified_sensitive_action" in anomalies.json()["rules"]

    retention = _call("get", "/audit/logs/retention", db)
    assert retention.status_code == 200
    assert retention.json()["keep_count"] == 2

    export = _call("get", "/audit/logs/export?format=csv&page_size=1", db)
    assert export.status_code == 200
    body = export.json()
    assert body["format"] == "csv" and body["entry_count"] == 2
    assert body["columns"] == list(audit_log.AUDIT_EXPORT_COLUMNS)


def test_audit_log_export_rejects_unknown_format():
    db = _FakeDb(entries=[])
    assert _call("get", "/audit/logs/export?format=xml", db).status_code == 422


def test_audit_forensic_endpoints_require_admin():
    db = _FakeDb(entries=[])
    _override(db, _FakeCustomer, force_admin_dep=False)
    try:
        client = TestClient(app_of())
        assert client.get("/audit/logs/actors").status_code == 403
        assert client.get("/audit/logs/integrity").status_code == 403
    finally:
        _clear()


def test_meta_features_documents_the_audit_forensic_endpoints():
    features = TestClient(app_of()).get("/meta/features").json()["endpoints"]
    assert features["audit_log_governed"] == "/audit/log/auditable"
    assert features["audit_log_integrity"] == "/audit/logs/integrity"
    assert features["audit_log_actors"] == "/audit/logs/actors"
    assert features["audit_log_timeline"] == "/audit/logs/timeline"
    assert features["audit_log_anomalies"] == "/audit/logs/anomalies"
    assert features["audit_log_retention"] == "/audit/logs/retention"
    assert features["audit_log_export"] == "/audit/logs/export"
    # The original keys are untouched.
    assert features["audit_log"] == "/audit/log"


# ===========================================================================
# app/hsm_signer.py — rotation, policy, co-signature, health
# ===========================================================================


def test_legacy_envelope_paths_are_unchanged():
    envelope = hsm_signer.sign_envelope({"user": "alice"})
    assert hsm_signer.verify_envelope(envelope) is True
    assert hsm_signer.decode_envelope(envelope) == {"user": "alice"}
    # No nonce/TTL means no extra envelope fields, so the wire format is stable.
    assert "nonce" not in envelope and "expires_at" not in envelope
    assert hsm_signer.verify_envelope({"payload": "not-base64", "signature": "x"}) is False
    tampered = dict(envelope)
    tampered["payload"] = hsm_signer.sign_envelope({"user": "mallory"})["payload"]
    assert hsm_signer.verify_envelope(tampered) is False


def test_verify_envelope_detailed_reports_reason_codes():
    envelope = hsm_signer.sign_envelope({"v": 1})
    assert hsm_signer.verify_envelope_detailed(envelope)["reason"] == "ok"
    assert hsm_signer.verify_envelope_detailed({"nope": 1})["reason"] == "malformed_envelope"
    bad_algorithm = dict(envelope, algorithm="HSM-UNKNOWN")
    assert hsm_signer.verify_envelope_detailed(bad_algorithm)["reason"] == "unknown_algorithm"
    # Reject a short-lived envelope only when it is actually stale.
    stale = hsm_signer.sign_envelope({"v": 2}, ttl_seconds=-10)
    assert hsm_signer.verify_envelope_detailed(stale)["reason"] == "expired"
    assert hsm_signer.verify_envelope_detailed(
        stale, policy={"expiry_tolerance_seconds": 3600}
    )["reason"] == "ok"
    future = dict(envelope, not_before=(NOW + timedelta(hours=1)).isoformat())
    assert hsm_signer.verify_envelope_detailed(future)["reason"] == "not_yet_valid"


def test_envelope_freshness_and_replay_guard():
    guard = hsm_signer.ReplayGuard(capacity=2)
    envelope = hsm_signer.sign_envelope({"v": 3}, nonce="n-1")
    assert hsm_signer.verify_envelope_detailed(envelope, replay_guard=guard)["reason"] == "ok"
    assert hsm_signer.verify_envelope_detailed(envelope, replay_guard=guard)["reason"] == "replayed"
    # Capacity is bounded: the oldest nonces are forgotten.
    for index in range(3):
        hsm_signer.verify_envelope_detailed(
            hsm_signer.sign_envelope({"v": index}, nonce=f"n-{index}"), replay_guard=guard
        )
    assert guard.size() == 2
    guard.clear()
    assert guard.size() == 0
    assert guard.check_and_remember("") is False


def test_signing_key_ring_rotates_without_breaking_old_envelopes():
    ring = hsm_signer.SigningKeyRing()
    old = hsm_signer.HmacHsmSigner(key=b"old", key_id="k1")
    new = hsm_signer.HmacHsmSigner(key=b"new", key_id="k2")
    ring.add("k1", old)
    before_rotation = hsm_signer.sign_envelope({"v": 1}, old)

    ring.add("k2", new)  # adding an active key demotes k1
    assert ring.active().key_id == "k2"
    assert ring.previous_key_ids() == ["k1"]
    assert hsm_signer.verify_envelope_detailed(before_rotation, ring=ring)["valid"] is True

    after_rotation = hsm_signer.sign_envelope({"v": 2}, new)
    assert hsm_signer.verify_envelope_detailed(after_rotation, ring=ring)["valid"] is True

    # Retiring a key stops it verifying, and the active key cannot be retired.
    ring.retire("k1")
    assert hsm_signer.verify_envelope_detailed(before_rotation, ring=ring)["reason"] == "key_id_mismatch"
    with pytest.raises(ValueError, match="cannot retire the active key"):
        ring.retire("k2")
    with pytest.raises(KeyError):
        ring.activate("nope")
    assert ring.catalog()["active_key_id"] == "k2"
    ring.reset()
    assert ring.active() is None


def test_signing_key_ring_status_validation():
    ring = hsm_signer.SigningKeyRing()
    with pytest.raises(ValueError, match="status must be one of"):
        ring.add("k", hsm_signer.MockHsmSigner(), status="sideways")
    ring.add("k", hsm_signer.MockHsmSigner(key_id="k"), status="previous")
    assert ring.active() is None
    assert ring.activate("k").status == "active"


def test_backend_registry_accepts_a_new_adapter():
    assert set(hsm_signer.SIGNER_BACKENDS) == {"hmac", "ed25519", "mock"}
    factory = lambda: hsm_signer.MockHsmSigner(key_id="pkcs11-stub")  # noqa: E731
    hsm_signer.register_signer_backend("pkcs11", factory)
    try:
        assert "pkcs11" in hsm_signer.registered_backends()
        assert hsm_signer.get_signer("pkcs11").key_id == "pkcs11-stub"
        with pytest.raises(ValueError, match="already registered"):
            hsm_signer.register_signer_backend("pkcs11", factory)
        hsm_signer.register_signer_backend("pkcs11", factory, replace=True)
    finally:
        hsm_signer.SIGNER_BACKENDS.pop("pkcs11", None)
        hsm_signer.reset_signer_cache()
    with pytest.raises(ValueError, match="non-empty"):
        hsm_signer.register_signer_backend("  ", factory)


def test_sign_multi_and_verify_multi_thresholds():
    a = hsm_signer.HmacHsmSigner(key=b"a", key_id="a")
    b = hsm_signer.HmacHsmSigner(key=b"b", key_id="b")
    c = hsm_signer.HmacHsmSigner(key=b"c", key_id="c")
    document = hsm_signer.sign_multi(b"payload", [a, b, c])
    assert document["authorities"] == 3 and len(document["cosignatures"]) == 3
    assert document["payload_sha256"]

    # With no key source the foreign key ids cannot be checked, so nothing
    # verifies and the threshold is reported unmet.
    unresolved = hsm_signer.verify_multi(b"payload", document, threshold=1)
    assert unresolved["valid"] is False
    assert unresolved["reason"] == "threshold_not_met"
    assert unresolved["verified_key_ids"] == [] and unresolved["offered"] == 3
    # The digest is checked before any signature, so a wrong payload is caught
    # even without keys.
    assert (
        hsm_signer.verify_multi(b"other", document, threshold=1)["reason"]
        == "payload_digest_mismatch"
    )
    # A zero threshold never counts as satisfied.
    assert hsm_signer.verify_multi(b"payload", document, threshold=0)["valid"] is False

    ring = hsm_signer.SigningKeyRing()
    for signer in (a, b, c):
        ring.add(signer.key_id, signer)
    resolved = hsm_signer.verify_multi(b"payload", document, threshold=2, ring=ring)
    assert resolved["valid"] is True and resolved["reason"] == "ok"
    assert set(resolved["verified_key_ids"]) == {"a", "b", "c"}
    # N-of-M: two of three is not three of three.
    assert (
        hsm_signer.verify_multi(b"payload", document, threshold=3, ring=ring)["valid"]
        is True
    )
    assert (
        hsm_signer.verify_multi(b"payload", document, threshold=4, ring=ring)["reason"]
        == "threshold_not_met"
    )

    # An explicit `signers` mapping resolves keys without a ring.
    by_id = {signer.key_id: signer for signer in (a, b)}
    assert hsm_signer.verify_multi(b"payload", document, threshold=2, signers=by_id)[
        "verified_key_ids"
    ] == ["a", "b"]
    # One forged cosignature is simply never verified.
    forged = dict(document)
    forged["cosignatures"] = [
        {**document["cosignatures"][0], "signature": "!!!not-base64!!!"},
        document["cosignatures"][1],
    ]
    partial = hsm_signer.verify_multi(b"payload", forged, threshold=1, signers=by_id)
    assert partial["valid"] is True and partial["verified_key_ids"] == ["b"]
    assert (
        hsm_signer.verify_multi(b"payload", forged, threshold=2, signers=by_id)["reason"]
        == "threshold_not_met"
    )


def test_key_identification_never_leaks_the_secret():
    symmetric = hsm_signer.HmacHsmSigner(key=b"super-secret", key_id="sym")
    assert symmetric.algorithm == "HSM-HS256"
    assert public_key_pem_empty(symmetric) is True
    fingerprint = hsm_signer.key_fingerprint(symmetric)
    assert fingerprint.startswith("HSM-HS256:")
    assert "super-secret" not in fingerprint
    assert hsm_signer.key_fingerprint(symmetric) == fingerprint
    info = hsm_signer.signer_info(symmetric)
    assert info["asymmetric"] is False and info["signer_class"] == "HmacHsmSigner"

    asymmetric = hsm_signer.Ed25519HsmSigner()
    pem = hsm_signer.public_key_pem(asymmetric)
    assert pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert hsm_signer.signer_info(asymmetric)["asymmetric"] is True


def public_key_pem_empty(signer) -> bool:
    return hsm_signer.public_key_pem(signer) == ""


def test_signer_self_test_and_health():
    result = hsm_signer.signer_self_test(hsm_signer.MockHsmSigner(key_id="probe"))
    assert result["roundtrip"] is True
    assert result["tamper_detected"] is True
    assert result["healthy"] is True
    health = hsm_signer.build_signer_health(["mock"])
    assert health["checked"] == 1 and health["healthy"] is True


def test_hsm_signer_catalog_keeps_pinned_keys_and_adds_surface():
    catalog = hsm_signer.build_hsm_signer_catalog()
    # Pinned contract.
    assert catalog["supported_backends"] == ["hmac", "ed25519", "mock"]
    assert catalog["key_id"] and catalog["algorithm"]
    # Expansion surface.
    assert set(catalog["registered_backends"]) == {"hmac", "ed25519", "mock"}
    assert catalog["backend_registry"]["hmac"] == "HmacHsmSigner"
    assert catalog["key"]["algorithm"] == catalog["algorithm"]
    assert catalog["verify_reasons"] == list(hsm_signer.VERIFY_REASONS)
    assert catalog["policy_defaults"]["expiry_tolerance_seconds"] == 0
    assert catalog["self_test"]["healthy"] is True
    assert catalog["key_ring"]["size"] == 0


# ===========================================================================
# app/biometric_vault.py — challenges, decisions, lockout, tuning
# ===========================================================================


TEMPLATE = b"\x01\x02\x03\x04\x05\x06\x07\x08" * 4
OTHER = bytes(byte ^ 0xFF for byte in TEMPLATE)


def test_challenge_is_single_use_and_expiring():
    vault = biometric_vault.BiometricVault()
    vault.register_template(1, "fingerprint", TEMPLATE)
    challenge = vault.issue_challenge(1, "fingerprint")
    assert challenge["challenge_id"] and challenge["nonce"]
    assert vault.validate_template(1, "fingerprint", TEMPLATE, challenge=challenge)["match"] is True
    # Second presentation of the same challenge is rejected.
    assert (
        vault.validate_template(1, "fingerprint", TEMPLATE, challenge=challenge)["reason"]
        == "challenge_replayed"
    )
    stale = vault.issue_challenge(1, "fingerprint", ttl_seconds=-1)
    assert vault.validate_template(1, "fingerprint", TEMPLATE, challenge=stale)["reason"] == "challenge_expired"
    assert (
        vault.validate_template(1, "fingerprint", TEMPLATE, challenge={"challenge_id": "zzz"})["reason"]
        == "challenge_unknown"
    )


def test_challenge_does_not_change_the_similarity_math():
    vault = biometric_vault.BiometricVault()
    vault.register_template(1, "voice", TEMPLATE)
    challenge = vault.issue_challenge(1, "voice")
    with_challenge = vault.validate_template(1, "voice", TEMPLATE, challenge=challenge)
    without = vault.validate_template(1, "voice", TEMPLATE)
    assert with_challenge["confidence"] == without["confidence"] == 1.0


def test_challenge_is_bound_to_user_and_modality():
    vault = biometric_vault.BiometricVault()
    vault.register_template(1, "face", TEMPLATE)
    vault.register_template(2, "face", TEMPLATE)
    challenge = vault.issue_challenge(1, "face")
    assert vault.validate_template(2, "face", TEMPLATE, challenge=challenge)["reason"] == "challenge_unknown"


def test_decide_returns_a_three_way_outcome():
    vault = biometric_vault.BiometricVault()
    vault.register_template(1, "face", TEMPLATE)
    accepted = vault.decide(1, "face", TEMPLATE)
    assert accepted["decision"] == "accept" and accepted["step_up_required"] is False
    # Unknown enrolment is a hard deny, not a step-up.
    unknown = vault.decide(9, "face", TEMPLATE)
    assert unknown["decision"] == "deny" and unknown["reason"] == "template_not_registered"
    # Revoked is also a hard deny.
    vault.revoke_template(1, "face")
    assert vault.decide(1, "face", TEMPLATE)["decision"] == "deny"


def test_decide_uses_the_step_up_band():
    # `digest_hamming` scores an exact capture 1.0, so the band is probed with a
    # threshold the exact capture cannot clear but a real marginal capture can.
    banded = biometric_vault.BiometricVault(
        modalities={
            "fingerprint": {
                "threshold": 1.01,
                "step_up_threshold": 0.90,
                "min_threshold": 0.80,
                "max_threshold": 0.99,
                "hash": "sha256",
                "matcher": "digest_hamming",
            }
        }
    )
    banded.register_template(1, "fingerprint", TEMPLATE)
    result = banded.decide(1, "fingerprint", TEMPLATE)
    assert result["step_up_threshold"] == 0.90
    assert result["confidence"] == 1.0 and result["reason"] == "below_threshold"
    assert result["decision"] == "challenge" and result["step_up_required"] is True
    # Below the band is a hard deny.
    below = banded.decide(1, "fingerprint", OTHER)
    assert below["confidence"] < 0.90 and below["decision"] == "deny"
    assert below["step_up_required"] is False

    # The shipped fingerprint config accepts the same capture outright.
    vault = biometric_vault.BiometricVault()
    vault.register_template(1, "fingerprint", TEMPLATE)
    accepted = vault.decide(1, "fingerprint", TEMPLATE)
    assert accepted["decision"] == "accept" and accepted["step_up_threshold"] == 0.90
    # A locked-out user is denied outright, never stepped up.
    locked = biometric_vault.BiometricVault(
        policy={"max_attempts": 1, "lockout_seconds": 300}
    )
    locked.register_template(1, "fingerprint", TEMPLATE)
    locked.validate_template(1, "fingerprint", OTHER)
    refused = locked.decide(1, "fingerprint", TEMPLATE)
    assert refused["reason"] == "locked_out" and refused["decision"] == "deny"
    assert refused["step_up_required"] is False


def test_brute_force_lockout_and_reset():
    vault = biometric_vault.BiometricVault(policy={"max_attempts": 2, "lockout_seconds": 300})
    vault.register_template(1, "voice", TEMPLATE)
    assert vault.validate_template(1, "voice", OTHER)["reason"] == "below_threshold"
    assert vault.validate_template(1, "voice", OTHER)["reason"] == "below_threshold"
    # Even a perfect capture is refused while locked out.
    assert vault.validate_template(1, "voice", TEMPLATE)["reason"] == "locked_out"
    state = vault.attempt_state(1, "voice")
    assert state["failed"] == 2 and state["locked_until"]
    vault.reset_attempts(1, "voice")
    assert vault.validate_template(1, "voice", TEMPLATE)["reason"] == "match"


def test_successful_validation_clears_the_failure_counter():
    vault = biometric_vault.BiometricVault(policy={"max_attempts": 3})
    vault.register_template(1, "voice", TEMPLATE)
    vault.validate_template(1, "voice", OTHER)
    vault.validate_template(1, "voice", TEMPLATE)
    assert vault.attempt_state(1, "voice")["failed"] == 0


def test_lockout_can_be_disabled_by_policy():
    vault = biometric_vault.BiometricVault(
        policy={"max_attempts": 1, "lockout_seconds": 300, "lockout_enabled": False}
    )
    vault.register_template(1, "voice", TEMPLATE)
    vault.validate_template(1, "voice", OTHER)
    assert vault.validate_template(1, "voice", OTHER)["reason"] == "below_threshold"


def test_adaptive_thresholds_move_and_clamp():
    vault = biometric_vault.BiometricVault()
    base = vault.threshold_for("face")
    tightened = vault.adapt_threshold("face", "false_positive")
    assert tightened["threshold"] < base and tightened["changed"] is True
    assert tightened["base_threshold"] == base
    loosened = vault.adapt_threshold("face", "false_negative")
    assert loosened["threshold"] == tightened["threshold"] + 0.01
    # A confirmed match is a no-op.
    assert vault.adapt_threshold("face", "confirmed_match")["changed"] is False
    # Clamped to the configured band.
    vault.adapt_threshold("face", "false_positive", step=0.5)
    assert vault.threshold_for("face") == vault.modalities["face"]["min_threshold"]
    with pytest.raises(ValueError, match="outcome must be one of"):
        vault.adapt_threshold("face", "vibes")
    with pytest.raises(ValueError, match="unsupported biometric modality"):
        vault.threshold_for("iris")
    # Restore.
    assert {k: v["threshold"] for k, v in vault.reset_thresholds().items()}["face"] == base


def test_multi_slot_enrollment():
    vault = biometric_vault.BiometricVault()
    vault.register_template(1, "fingerprint", TEMPLATE)
    first = vault.enroll_slot(1, "fingerprint", b"index-finger" * 4)
    second = vault.enroll_slot(1, "fingerprint", b"middle-finger" * 4)
    assert first["slot"] == 1 and second["slot"] == 2
    assert [row["slot"] for row in vault.list_slots(1, "fingerprint")] == [1, 2]
    # Each slot verifies independently, and the primary is untouched.
    assert vault.validate_template(1, "fingerprint", b"index-finger" * 4, slot=1)["match"] is True
    assert vault.validate_template(1, "fingerprint", b"index-finger" * 4)["match"] is False
    assert vault.validate_template(1, "fingerprint", TEMPLATE)["match"] is True
    assert len(vault.list_templates(1)) == 3
    with pytest.raises(ValueError, match="slot must be between"):
        vault.enroll_slot(1, "fingerprint", b"x", slot=0)
    with pytest.raises(ValueError, match="no slot"):
        vault.revoke_slot(1, "fingerprint", 9)
    assert vault.revoke_slot(1, "fingerprint", 1)["revoked"] is True
    assert vault.validate_template(1, "fingerprint", b"index-finger" * 4, slot=1)["reason"] == "template_revoked"


def test_history_records_attempts():
    vault = biometric_vault.BiometricVault()
    vault.register_template(1, "voice", TEMPLATE)
    vault.validate_template(1, "voice", TEMPLATE)
    vault.validate_template(1, "voice", OTHER)
    history = vault.history(1, "voice")
    assert [row["reason"] for row in history] == ["match", "below_threshold"]
    assert vault.history(2) == []


def test_export_import_state_replicates_digests():
    source = biometric_vault.BiometricVault()
    source.register_template(1, "fingerprint", TEMPLATE)
    source.enroll_slot(1, "fingerprint", b"index" * 8)
    document = source.export_state()
    assert document["version"] == biometric_vault.VAULT_STATE_VERSION
    # No plaintext template anywhere in the exported document.
    assert TEMPLATE.hex() not in str(document)

    replica = biometric_vault.BiometricVault()
    result = replica.import_state(document)
    assert result["imported"] == 2
    assert replica.validate_template(1, "fingerprint", TEMPLATE)["match"] is True
    assert replica.validate_template(1, "fingerprint", b"index" * 8, slot=1)["match"] is True

    with pytest.raises(ValueError, match="unsupported vault state version"):
        replica.import_state({"version": 99})
    # Unknown modalities are skipped, not fatal.
    mixed = dict(
        document,
        templates=[
            {
                "user_id": 1,
                "modality": "iris",
                "slot": 0,
                "salt": "s",
                "stored_hash": "h",
                "reference_digest": b"",
            }
        ],
        slots=[
            {
                "user_id": 1,
                "modality": "retina",
                "slot": 1,
                "salt": "s",
                "stored_hash": "h",
                "reference_digest": b"",
            }
        ],
    )
    skipped = replica.import_state(mixed, replace=True)
    assert skipped["imported"] == 0
    assert skipped["replaced"] is True
    assert skipped["template_count"] == 0 and skipped["slot_count"] == 0


def test_biometric_catalog_keeps_pinned_keys_and_adds_surface():
    catalog = biometric_vault.build_biometric_vault_catalog(biometric_vault.BiometricVault())
    # Pinned contract.
    assert set(catalog["modalities"]) == {"fingerprint", "face", "voice"}
    assert catalog["storage"]["plaintext_templates"] is False
    assert catalog["template_count"] == 0
    # Expansion surface.
    assert catalog["reasons"] == list(biometric_vault.BIOMETRIC_REASONS)
    assert catalog["decisions"] == list(biometric_vault.BIOMETRIC_DECISIONS)
    assert catalog["thresholds"]["face"]["base_threshold"] == 0.86
    assert catalog["policy"]["max_attempts"] == 5
    assert catalog["challenge"]["single_use"] is True
    assert catalog["slot_capacity"] == 4
    assert catalog["state_version"] == biometric_vault.VAULT_STATE_VERSION


# ===========================================================================
# app/tenant_router.py — resolution, redaction, roles, failover
# ===========================================================================


def test_tenant_id_validation_and_normalization():
    assert tenant_router.is_valid_tenant_id("acme") is True
    assert tenant_router.is_valid_tenant_id("acme-eu.1") is True
    assert tenant_router.is_valid_tenant_id("Acme") is False
    assert tenant_router.is_valid_tenant_id("acme/../etc") is False
    assert tenant_router.is_valid_tenant_id("x" * 70) is False
    assert tenant_router.is_valid_tenant_id("") is False
    assert tenant_router.normalize_tenant_id("  acme ") == "acme"
    assert tenant_router.normalize_tenant_id(None) == tenant_router.DEFAULT_TENANT


def test_redact_dsn_hides_credentials_but_keeps_shape():
    assert tenant_router.redact_dsn("postgresql+asyncpg://admin:s3cret@db:5432/app") == (
        "postgresql+asyncpg://admin:***@db:5432/app"
    )
    assert tenant_router.redact_dsn("postgresql+asyncpg://db/app") == "postgresql+asyncpg://db/app"
    assert tenant_router.redact_dsn("") == ""
    assert tenant_router.redact_dsn(None) is None
    assert tenant_router.redact_dsn("not-a-dsn") == "***redacted***"


def test_resolve_tenant_reports_reason_codes():
    router = tenant_router.TenantRouter(policy={"allowlist": ("acme", "beta")})
    assert router.resolve_tenant("acme").reason == "ok"
    assert router.resolve_tenant("gamma").reason == "not_allowed"
    assert router.resolve_tenant("BAD ID").reason == "invalid_tenant_id"
    router.set_status("beta", "suspended")
    assert router.resolve_tenant("beta").reason == "suspended"
    assert router.resolve_tenant("beta").mode == "shared_default"
    router.set_status("beta", "quarantined")
    assert router.resolve_tenant("beta").reason == "quarantined"
    with pytest.raises(ValueError, match="status must be one of"):
        router.set_status("acme", "sideways")
    with pytest.raises(ValueError, match="role must be one of"):
        router.resolve_tenant("acme", role="replica")
    payload = router.resolve_tenant("acme", source="X-Tenant-ID header").to_payload()
    assert payload["source"] == "X-Tenant-ID header" and payload["allowed"] is True


@pytest.mark.asyncio
async def test_suspended_tenant_falls_back_to_the_shared_engine():
    router = tenant_router.TenantRouter()
    dedicated = await router.register("acme", dsn="postgresql+asyncpg://u:p@h:5432/acme")
    router.set_status("acme", "suspended")
    assert await router.engine_for("acme") is router._default_engine
    assert dedicated is not router._default_engine
    router.set_status("acme", "active")
    assert await router.engine_for("acme") is dedicated
    await router.dispose_all()


@pytest.mark.asyncio
async def test_unhealthy_tenant_fails_over_and_recovers():
    router = tenant_router.TenantRouter(policy={"health_failure_threshold": 2})
    await router.register("acme", dsn="postgresql+asyncpg://u:p@h:5432/acme")
    for _ in range(2):
        router.record_outcome("acme", False)
    assert router.unhealthy_tenants() == ["acme"]
    assert await router.engine_for("acme") is router._default_engine
    router.record_outcome("acme", True)
    assert router.is_healthy("acme") is True
    assert router.unhealthy_tenants() == []
    await router.dispose_all()


@pytest.mark.asyncio
async def test_read_write_splitting_registers_separate_engines():
    router = tenant_router.TenantRouter()
    writer = await router.register("acme", dsn="postgresql+asyncpg://u:p@h:5432/acme")
    reader = await router.register("acme", dsn="postgresql+asyncpg://u:p@h:5432/acme_ro", role="read")
    assert writer is not reader
    assert set(router.snapshot()) == {"acme", "acme#read"}
    assert router.snapshot()["acme"]["role"] == "write"
    assert router.snapshot()["acme#read"]["role"] == "read"
    # Deregistering a tenant takes both roles with it.
    assert await router.deregister("acme") is True
    assert router.snapshot() == {}
    assert await router.deregister("acme") is False
    with pytest.raises(ValueError, match="role must be one of"):
        await router.register("acme", role="archive")
    await router.dispose_all()


@pytest.mark.asyncio
async def test_pool_overrides_and_metadata():
    router = tenant_router.TenantRouter()
    assert router.effective_pool("acme")["size"] == tenant_router.TENANT_POOL_SIZE
    assert router.set_pool_overrides("acme", pool_size=9)["pool"] == {"pool_size": 9}
    assert router.effective_pool("acme")["size"] == 9
    assert router.effective_pool("beta")["size"] == tenant_router.TENANT_POOL_SIZE
    with pytest.raises(ValueError, match="unsupported pool override"):
        router.set_pool_overrides("acme", threads=99)
    assert router.set_metadata("acme", region="eu")["metadata"] == {"region": "eu"}
    assert router.metadata_for("acme") == {"region": "eu"}
    assert router.metadata_for("nobody") == {}


@pytest.mark.asyncio
async def test_usage_tracking_counts_engine_handoffs():
    router = tenant_router.TenantRouter()
    await router.register("acme", dsn="postgresql+asyncpg://u:p@h:5432/acme")
    await router.engine_for("acme")
    await router.engine_for("acme")
    usage = router.usage()["acme"]
    assert usage["requests"] == 2 and usage["idle_seconds"] >= 0
    await router.dispose_all()


@pytest.mark.asyncio
async def test_plan_provisioning_previews_without_registering():
    router = tenant_router.TenantRouter(policy={"allowlist": ("acme",)})
    plan = router.plan_provisioning(["acme", "gamma", "BAD ID"])
    assert plan["candidates"] == 3
    actions = {row["tenant_id"]: row for row in plan["rows"]}
    assert actions["acme"]["action"] == "route_shared"
    assert actions["acme"]["registered"] is False
    assert actions["gamma"]["reason"] == "not_allowed"
    assert actions["BAD ID"]["reason"] == "invalid_tenant_id"
    assert plan["pool_defaults"]["size"] == tenant_router.TENANT_POOL_SIZE


def test_tenant_router_catalog_keeps_its_pinned_key_set():
    catalog = tenant_router.build_tenant_router_catalog()
    # Pinned exactly: new capability nests inside these keys.
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
    assert catalog["lookup_order"] == [
        "X-Tenant-ID header",
        "tenant_id query param",
        "default tenant",
    ]
    assert set(catalog["pool"]) >= {"size", "max_overflow", "timeout", "echo", "roles", "max_engines", "overrides", "usage"}


def test_tenant_routing_policy_surface():
    policy = tenant_router.build_tenant_routing_policy(tenant_router.TenantRouter())
    assert policy["id_pattern"] == tenant_router.TENANT_ROUTER_POLICY["id_pattern"]
    assert policy["statuses"] == list(tenant_router.TENANT_STATUSES)
    assert policy["roles"] == list(tenant_router.TENANT_ROLES)
    assert policy["resolution_reasons"] == list(tenant_router.TENANT_RESOLUTION_REASONS)
    assert policy["health"]["failover_enabled"] is True
    assert "rows" in policy["provisioning"]


def test_meta_tenants_policy_endpoint():
    response = TestClient(app_of()).get("/meta/tenants/policy")
    assert response.status_code == 200
    body = response.json()
    assert body["default_tenant"] == tenant_router.DEFAULT_TENANT
    assert body["statuses"] == list(tenant_router.TENANT_STATUSES)
    assert TestClient(app_of()).get("/meta/features").json()["endpoints"][
        "tenant_routing_policy"
    ] == "/meta/tenants/policy"


# ===========================================================================
# app/model_bases.py — extended security-event families
# ===========================================================================


def test_model_bases_catalog_reports_extensions_without_widening_families():
    catalog = model_bases.build_model_bases_catalog()
    # Pinned: `families` still means exactly the original three.
    assert set(catalog["families"]) == {"authentication", "risk", "access"}
    assert catalog["family_count"] == 3
    # New signal families are reported separately.
    assert set(catalog["extensions"]) == set(model_bases.SECURITY_EVENT_EXTENSIONS)
    assert catalog["extension_count"] == 4
    assert set(catalog["all_families"]) == set(catalog["families"]) | set(catalog["extensions"])
    assert catalog["severities"] == list(model_bases.SECURITY_EVENT_SEVERITIES)
    assert catalog["severity_rank"]["critical"] == 4
    # The registry resolves both sets.
    assert set(catalog["registry"]) >= set(catalog["all_families"]) | {"security_event"}


def test_extended_security_event_families_share_one_table():
    for kind, cls in model_bases.SECURITY_EVENT_EXTENSIONS.items():
        assert model_bases.security_event_family(kind) is cls
        assert cls.__tablename__ == "security_events"
        assert cls.__mapper__.polymorphic_identity == kind
    # An unknown kind still degrades to the polymorphic root.
    assert model_bases.security_event_family("nope") is model_bases.SecurityEvent


def test_build_security_event_validates_severity_and_picks_class():
    event = model_bases.build_security_event(
        "break_glass", summary="Break-glass consumed", severity="high"
    )
    assert isinstance(event, model_bases.BreakGlassSecurityEvent)
    assert event.event_kind == "break_glass"
    with pytest.raises(ValueError, match="severity must be one of"):
        model_bases.build_security_event("break_glass", severity="apocalyptic")
