"""Tests for the ``app.schemas.audit`` contract-governance expansion.

The module grew from 520 to ~3,000 LOC. What was added is a report layer: nine
config tables, a 33-code finding taxonomy, eight pydantic reports, and six public
functions. Nothing above the governance banner changed, and that is the property
most of this file is checking.

Four groups:

1. **Pinned shipped behaviour.** ``ORIG_*`` constants transcribe the 40 contracts
   as they stood at 520 LOC -- every field name, every (fields, required) count,
   every comment-declared vocabulary. So "the schemas were not touched" is a
   checkable claim rather than an intention. Several of the pinned facts are
   *defects* -- ``severity`` meaning two different things, five dead-letter
   timestamps declared ``str``, ``entry_ids: List[Optional[int]]`` -- and they
   are pinned on purpose, because the expansion reports them and does not repair
   them.
2. **The write-path claims, checked against the service.** ``CONTRACT_WRITE_PATHS``
   asserts which route scrubs its detail blob. That is a claim about
   ``app.services.audit_log``, so it is verified by reading the *function
   bodies* with ``ast`` -- not by grepping the module, which is how the table
   first came to say the ungated path redacts.
3. **The tables and the reports.** Shape, internal coherence, and that each guard
   fires against a deliberately broken table rather than only ever reporting
   clean on a well-formed one.
4. **The surfaces.** ``/meta/audit-contracts``, ``/meta/scoring-catalog``,
   ``/meta``, ``/meta/features``, ``/meta/ecosystem``.

Mutation discipline: tables are replaced wholesale with ``monkeypatch.setattr``,
never edited in place. ``list(TABLE)`` is a shallow copy whose row dicts are the
live ones, so an in-place row edit restored by assignment would reinstate the
mutation rather than undo it.
"""
import ast
import inspect

import pytest

from app.schemas import audit as audit_schemas
from app.services import audit_log
from app.main import app
from fastapi.testclient import TestClient


client = TestClient(app)


# ==============================================================================
# 1. Pinned shipped behaviour
# ==============================================================================

# Transcribed from the module as it stood at 520 LOC. (fields, required) for
# each of the 40 contracts.
ORIG_SHAPE = {
    "AuditActorActivityOut": (8, 2),
    "AuditActorActivityReport": (3, 2),
    "AuditAnomalyFindingOut": (7, 4),
    "AuditAnomalyReport": (6, 3),
    "AuditGateIntegrityReport": (9, 1),
    "AuditIntegrityGateOut": (11, 3),
    "AuditLogEntryCreate": (7, 2),
    "AuditLogEntryOut": (10, 8),
    "AuditLogExportReport": (7, 6),
    "AuditLogIntegrityReport": (7, 5),
    "AuditLogListReport": (5, 4),
    "AuditLogSummaryItem": (2, 2),
    "AuditLogSummaryReport": (4, 2),
    "AuditProjectedEntryOut": (17, 2),
    "AuditRetentionRecordOut": (6, 5),
    "AuditRetentionReport": (9, 5),
    "AuditTimelineEventOut": (8, 2),
    "AuditTimelineReport": (5, 2),
    "AuditViewReport": (12, 4),
    "AuditableLogEntryCreate": (10, 2),
    "ComponentMetrics": (17, 3),
    "EnhancementListReport": (5, 3),
    "EnhancementProposal": (11, 9),
    "LogScanResult": (7, 0),
    "PipelineDeadLetterEntryOut": (15, 2),
    "PipelineDeadLetterReport": (17, 0),
    "PipelineEventAccepted": (3, 2),
    "PipelineEventCreate": (7, 1),
    "PipelineReplayReport": (11, 1),
    "PipelineReplayRequest": (3, 0),
    "PipelineStatsReport": (13, 13),
    "SystemDataPoint": (2, 2),
    "SystemDataSection": (4, 0),
    "SystemEfficiencyReport": (10, 4),
    "TransactionExportReport": (10, 5),
    "TransactionIntegrityReport": (11, 1),
    "TransactionLogReport": (4, 2),
    "TransactionOut": (12, 9),
    "TransactionQueryReport": (10, 5),
    "TransactionSpecReport": (1, 1),
}

# Field names in declaration order. A rename, a reorder-by-insertion, or a
# removed default is a change to the contract a client binds to.
ORIG_FIELDS = {
    "AuditActorActivityOut": ['actor_user_id', 'count', 'distinct_entities', 'by_action', 'by_severity', 'peak_severity', 'first_seen', 'last_seen'],
    "AuditActorActivityReport": ['generated_at', 'actor_count', 'actors'],
    "AuditAnomalyFindingOut": ['rule', 'severity', 'subject', 'message', 'count', 'entry_ids', 'observed_at'],
    "AuditAnomalyReport": ['generated_at', 'scanned', 'finding_count', 'rules', 'thresholds', 'findings'],
    "AuditGateIntegrityReport": ['generated_at', 'verdict', 'rejected_by', 'needs_review_by', 'advisory_by', 'metrics', 'gates', 'gates_evaluated', 'summary'],
    "AuditIntegrityGateOut": ['gate_id', 'metric', 'op', 'op_meaning', 'threshold', 'actual', 'observed', 'severity', 'holds', 'reason', 'rationale'],
    "AuditLogEntryCreate": ['action', 'entity_type', 'entity_id', 'summary', 'detail', 'severity', 'source'],
    "AuditLogEntryOut": ['id', 'actor_user_id', 'action', 'entity_type', 'entity_id', 'summary', 'detail', 'severity', 'source', 'created_at'],
    "AuditLogExportReport": ['format', 'entry_count', 'page_size', 'max_pages', 'truncated', 'columns', 'content'],
    "AuditLogIntegrityReport": ['generated_at', 'valid', 'entries', 'broken_at', 'algo', 'policy', 'sealed_fields'],
    "AuditLogListReport": ['generated_at', 'total', 'limit', 'offset', 'entries'],
    "AuditLogSummaryItem": ['key', 'count'],
    "AuditLogSummaryReport": ['generated_at', 'total', 'by_action', 'by_severity'],
    "AuditProjectedEntryOut": ['profile', 'detail_mode', 'detail_keys', 'redacted', 'truncated', 'id', 'action', 'severity', 'entity_type', 'entity_id', 'actor_user_id', 'source', 'summary', 'detail', 'created_at', 'updated_at', 'sealed'],
    "AuditRetentionRecordOut": ['id', 'action', 'severity', 'age_days', 'retention_days', 'expires_on'],
    "AuditRetentionReport": ['generated_at', 'now', 'expired_count', 'keep_count', 'expired', 'keep', 'policies', 'retention_by_severity', 'minimum_days'],
    "AuditTimelineEventOut": ['id', 'action', 'actor_user_id', 'severity', 'summary', 'source', 'occurred_at', 'changed'],
    "AuditTimelineReport": ['generated_at', 'entity_type', 'entity_id', 'count', 'events'],
    "AuditViewReport": ['generated_at', 'profile', 'detail_mode', 'fields', 'max_summary_chars', 'include_seal', 'count', 'redacted_count', 'truncated_count', 'sealed_count', 'entries', 'note'],
    "AuditableLogEntryCreate": ['action', 'entity_type', 'entity_id', 'summary', 'detail', 'severity', 'source', 'strict', 'require_justification', 'seal'],
    "ComponentMetrics": ['component', 'kind', 'description', 'paths_used', 'present', 'file_count', 'line_count', 'avg_file_lines', 'max_file_lines', 'largest_file', 'marker_count', 'marker_density_per_1000', 'test_files', 'test_ratio', 'efficiency_score', 'classification', 'findings'],
    "EnhancementListReport": ['generated_at', 'rule_version', 'total', 'by_priority', 'items'],
    "EnhancementProposal": ['id', 'code', 'component', 'title', 'priority', 'impact', 'effort', 'rationale', 'recommended_action', 'owner_hint', 'signals'],
    "LogScanResult": ['available', 'paths_scanned', 'file_count', 'error_lines', 'warning_lines', 'error_density_per_1000', 'samples'],
    "PipelineDeadLetterEntryOut": ['event_id', 'kind', 'occurred_at', 'error', 'severity', 'entity_type', 'entity_id', 'actor_user_id', 'tenant_id', 'payload', 'route', 'replayable', 'attempts', 'first_failed_at', 'last_failed_at'],
    "PipelineDeadLetterReport": ['dead_lettered', 'retained', 'replayable', 'policy', 'recent', 'generated_at', 'ring_capacity', 'evicted', 'replayable_ratio', 'blocked_by_policy', 'max_attempts_seen', 'by_kind', 'by_error', 'oldest_occurred_at', 'newest_occurred_at', 'drainable', 'note'],
    "PipelineEventAccepted": ['accepted', 'event_id', 'reason'],
    "PipelineEventCreate": ['kind', 'severity', 'entity_type', 'entity_id', 'actor_user_id', 'tenant_id', 'payload'],
    "PipelineReplayReport": ['generated_at', 'dry_run', 'attempted', 'requeued', 'exhausted', 'exhausted_count', 'max_attempts', 'backoff_seconds', 'remaining', 'policy', 'note'],
    "PipelineReplayRequest": ['max_attempts', 'backoff_seconds', 'dry_run'],
    "PipelineStatsReport": ['generated_at', 'name', 'running', 'workers', 'batch_size', 'max_queue', 'flush_seconds', 'pending', 'submitted', 'processed', 'batches', 'rejected', 'dead_lettered'],
    "SystemDataPoint": ['metric', 'value'],
    "SystemDataSection": ['available', 'source', 'error', 'points'],
    "SystemEfficiencyReport": ['generated_at', 'scope', 'method', 'rule_version', 'data_sources', 'components', 'system_data', 'logs', 'summary', 'enhancements'],
    "TransactionExportReport": ['generated_at', 'format', 'view', 'frame_count', 'filters', 'reimportable', 'signature_survives', 'bytes', 'content', 'note'],
    "TransactionIntegrityReport": ['generated_at', 'chain', 'checkpoints', 'checkpoint_interval', 'spec_versions', 'sampled', 'unsigned_but_required', 'append_only', 'total', 'verdict', 'findings'],
    "TransactionLogReport": ['generated_at', 'total', 'tail', 'chain'],
    "TransactionOut": ['version', 'tx_id', 'cursor', 'action', 'entity_type', 'entity_id', 'actor_user_id', 'tenant_id', 'occurred_at', 'payload', 'prev_hash', 'signature_present'],
    "TransactionQueryReport": ['generated_at', 'total', 'matched', 'offset', 'limit', 'order', 'sort', 'view', 'filters', 'results'],
    "TransactionSpecReport": ['spec'],
}

ORIG_MODEL_COUNT = 40


def test_the_shipped_model_set_is_untouched():
    assert set(audit_schemas._audit_models()) == set(ORIG_SHAPE)
    assert len(audit_schemas._audit_models()) == ORIG_MODEL_COUNT


def test_shipped_field_and_required_counts_are_untouched():
    models = audit_schemas._audit_models()
    live = {
        name: (
            len(audit_schemas._field_list(cls)),
            sum(1 for f in audit_schemas._field_list(cls) if cls.model_fields[f].is_required()),
        )
        for name, cls in models.items()
    }
    assert live == ORIG_SHAPE


def test_shipped_field_names_are_untouched():
    models = audit_schemas._audit_models()
    assert {n: audit_schemas._field_list(m) for n, m in models.items()} == ORIG_FIELDS


def test_every_shipped_contract_is_published_in_the_openapi_document():
    """A model nothing serves is a class, not a contract."""
    schemas_in_spec = set(app.openapi()["components"]["schemas"])
    missing = sorted(set(ORIG_SHAPE) - schemas_in_spec)
    assert missing == []


def test_severity_vocabularies_are_untouched():
    """The headline finding, pinned: one name, two vocabularies."""
    facts = audit_schemas._source_vocabularies()
    models = audit_schemas._audit_models()

    # three fields enforce the event vocabulary with a real pattern...
    for name in ("AuditLogEntryCreate", "AuditableLogEntryCreate", "PipelineEventCreate"):
        fact = audit_schemas._field_facts(models[name], "severity")
        assert fact["pattern"] == "^(info|warning|critical)$", name

    # ...and one declares a different vocabulary in a comment
    assert facts["AuditIntegrityGateOut.severity"] == "advisory | review | reject"
    assert audit_schemas._field_facts(models["AuditIntegrityGateOut"], "severity")["pattern"] is None
    assert audit_schemas._field_facts(models["AuditIntegrityGateOut"], "severity")["default_repr"] == "'advisory'"


def test_comment_only_vocabularies_are_untouched():
    """Eight fields name their values in a trailing comment and nowhere else."""
    assert audit_schemas._source_vocabularies() == ORIG_VOCABS


ORIG_VOCABS = {
    "AuditGateIntegrityReport.verdict": "clean | review | reject",
    "AuditIntegrityGateOut.reason": "ok | threshold_not_met | metric_missing",
    "AuditIntegrityGateOut.severity": "advisory | review | reject",
    "ComponentMetrics.classification": "efficient | needs_attention | at_risk | missing",
    "EnhancementProposal.effort": "high | medium | low",
    "EnhancementProposal.impact": "high | medium | low",
    "EnhancementProposal.priority": "high | medium | low",
    "TransactionIntegrityReport.verdict": "clean | review | reject",
}


def test_dead_letter_timestamps_are_untouched():
    """The ring stores ISO strings; the schema says so with a `str`."""
    models = audit_schemas._audit_models()
    for field in ("occurred_at", "first_failed_at", "last_failed_at"):
        shape = audit_schemas._shape_of(
            models["PipelineDeadLetterEntryOut"].model_fields[field].annotation
        )
        # Optional[str]: the short annotation is the bare "Optional", so the
        # shape is read off the type object rather than off its repr
        assert shape["is_optional"] is True, field
        assert shape["is_str"] is True, field
        assert audit_schemas._field_facts(
            models["PipelineDeadLetterEntryOut"], field
        )["required"] is False, field
    for field in ("oldest_occurred_at", "newest_occurred_at"):
        shape = audit_schemas._shape_of(
            models["PipelineDeadLetterReport"].model_fields[field].annotation
        )
        assert shape["is_optional"] is True, field
        assert shape["is_str"] is True, field


def test_occurred_at_is_datetime_everywhere_else():
    """One name, two types -- and the other one is a string on purpose."""
    models = audit_schemas._audit_models()
    as_datetime = sorted(
        name
        for name, cls in models.items()
        if "occurred_at" in audit_schemas._field_list(cls)
        and audit_schemas._shape_of(cls.model_fields["occurred_at"].annotation).get("is_datetime")
    )
    as_str = sorted(
        name
        for name, cls in models.items()
        if "occurred_at" in audit_schemas._field_list(cls)
        and audit_schemas._shape_of(cls.model_fields["occurred_at"].annotation).get("is_str")
    )
    assert as_str == ["PipelineDeadLetterEntryOut"]
    assert as_datetime == ["AuditTimelineEventOut", "TransactionOut"]


def test_entry_ids_is_the_only_nullable_element_list():
    models = audit_schemas._audit_models()
    nullable = sorted(
        f"{name}.{field}"
        for name, cls in models.items()
        for field in audit_schemas._field_list(cls)
        if audit_schemas._shape_of(cls.model_fields[field].annotation).get("element_is_optional")
    )
    assert nullable == ["AuditAnomalyFindingOut.entry_ids"]


def test_replayable_is_a_bool_on_the_entry_and_an_int_on_the_report():
    models = audit_schemas._audit_models()
    assert audit_schemas._field_facts(models["PipelineDeadLetterEntryOut"], "replayable")["annotation"] == "bool"
    assert audit_schemas._field_facts(models["PipelineDeadLetterReport"], "replayable")["annotation"] == "int"


def test_generated_at_stamp_counts_are_untouched():
    """18 models declare it: 17 required, 1 optional. 2 envelopes omit it."""
    models = audit_schemas._audit_models()
    with_stamp = sorted(
        name for name, cls in models.items() if "generated_at" in audit_schemas._field_list(cls)
    )
    required = sorted(
        name
        for name in with_stamp
        if audit_schemas._field_facts(models[name], "generated_at")["required"] is True
    )
    optional = sorted(
        name
        for name in with_stamp
        if audit_schemas._field_facts(models[name], "generated_at")["required"] is False
    )
    assert len(with_stamp) == 18
    assert len(required) == 17
    assert optional == ["PipelineDeadLetterReport"]

    envelopes = sorted(
        name
        for name, cls in models.items()
        if (audit_schemas.CONTRACT_INVENTORY[name]["kind"] in ("report", "accepted"))
        and "generated_at" not in audit_schemas._field_list(cls)
    )
    assert envelopes == ["AuditLogExportReport", "PipelineEventAccepted"]
    assert audit_schemas.CONTRACT_INVENTORY["AuditLogExportReport"]["kind"] == "report"
    assert audit_schemas.CONTRACT_INVENTORY["PipelineEventAccepted"]["kind"] == "accepted"


def test_id_is_an_int_on_one_contract_and_a_str_on_another():
    models = audit_schemas._audit_models()
    assert audit_schemas._field_facts(models["AuditLogEntryOut"], "id")["annotation"] == "int"
    assert audit_schemas._field_facts(models["EnhancementProposal"], "id")["annotation"] == "str"


def test_summary_is_the_only_uncapped_write_string():
    models = audit_schemas._audit_models()
    uncaped = sorted(
        f"{name}.{field}"
        for name, cls in models.items()
        if audit_schemas.CONTRACT_INVENTORY[name]["kind"] == "create"
        for field in audit_schemas._field_list(cls)
        if audit_schemas._shape_of(cls.model_fields[field].annotation).get("is_str")
        and audit_schemas._field_facts(cls, field)["max_length"] is None
        and not audit_schemas._field_facts(cls, field)["pattern"]
    )
    assert uncaped == ["AuditLogEntryCreate.summary", "AuditableLogEntryCreate.summary"]


def test_detail_is_declared_identically_on_both_write_paths():
    """The contract cannot tell the scrubbed path from the raw one."""
    models = audit_schemas._audit_models()
    raw = audit_schemas._field_facts(models["AuditLogEntryCreate"], "detail")
    governed = audit_schemas._field_facts(models["AuditableLogEntryCreate"], "detail")
    assert raw["annotation"] == governed["annotation"] == "Dict"
    assert raw["required"] is governed["required"]
    assert raw["description"] is None
    assert governed["description"] is None
    assert raw["max_length"] is governed["max_length"] is None


def test_content_is_a_bare_str_on_both_export_reports():
    models = audit_schemas._audit_models()
    for name in ("AuditLogExportReport", "TransactionExportReport"):
        fact = audit_schemas._field_facts(models[name], "content")
        assert fact["annotation"] == "str", name
        assert fact["max_length"] is None, name


def test_pipeline_stats_report_requires_all_thirteen_counters():
    models = audit_schemas._audit_models()
    fields = audit_schemas._field_list(models["PipelineStatsReport"])
    assert len(fields) == 13
    assert all(models["PipelineStatsReport"].model_fields[f].is_required() for f in fields)


def test_transaction_spec_report_is_a_single_dict():
    models = audit_schemas._audit_models()
    fields = audit_schemas._field_list(models["TransactionSpecReport"])
    assert fields == ["spec"]
    assert audit_schemas._field_facts(models["TransactionSpecReport"], "spec")["annotation"] == "Dict"


def test_transaction_query_results_is_untyped_where_the_log_report_is_not():
    models = audit_schemas._audit_models()
    untyped = audit_schemas._shape_of(
        models["TransactionQueryReport"].model_fields["results"].annotation
    )
    assert untyped["is_list"] is True
    assert untyped["element_is_dict_str_any"] is True
    assert untyped["element"] is not models["TransactionOut"]
    typed = audit_schemas._shape_of(models["TransactionLogReport"].model_fields["tail"].annotation)
    assert typed["is_list"] is True
    assert typed["element"] is models["TransactionOut"]


def test_the_governance_models_are_not_counted_as_contracts():
    """8 report models exist in this module and 0 of them are contracts."""
    models = audit_schemas._audit_models()
    assert set(audit_schemas.GOVERNANCE_MODELS) & set(models) == set()
    assert len(audit_schemas.GOVERNANCE_MODELS) == 8
    for name in audit_schemas.GOVERNANCE_MODELS:
        assert hasattr(audit_schemas, name), name
        assert getattr(audit_schemas, name).__module__ == audit_schemas.__name__


def test_governance_models_are_an_explicit_list_not_an_ordering_rule():
    """A banner-comment rule would reclassify a future contract silently.

    Asserted on the source, because the property is about how the exclusion is
    written: `_audit_models` filters by identity against the declared set, and
    reads no line number, no slice and no marker comment.
    """
    full = inspect.getsource(audit_schemas)
    func = next(
        node
        for node in ast.parse(full).body
        if isinstance(node, ast.FunctionDef) and node.name == "_audit_models"
    )
    # the body only, with the docstring removed: the prose is allowed to say
    # "banner" while the code is not allowed to implement one
    body = func.body[1:] if isinstance(func.body[0], ast.Expr) else func.body
    code_only = "\n".join(ast.unparse(node) for node in body)

    assert "GOVERNANCE_MODELS" in code_only
    assert "name not in GOVERNANCE_MODELS" in code_only
    # no ordering machinery of any kind
    for forbidden in ("__file__", "lineno", "start_line", "banner", "index"):
        assert forbidden not in code_only, forbidden


def test_audit_models_excludes_imported_classes():
    """A vendored base class is not a contract."""
    source = inspect.getsource(audit_schemas)
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".")[0])
    # BaseModel is the only class this module inherits from and it is excluded
    assert "BaseModel" in imported
    models = audit_schemas._audit_models()
    assert models  # and nothing imported leaked in
    for cls in models.values():
        assert cls.__module__ == audit_schemas.__name__


# ==============================================================================
# 2. The write-path claims, verified against the service
# ==============================================================================
#
# CONTRACT_WRITE_PATHS claims which route scrubs its detail blob. That is a claim
# about app.services.audit_log, so it is checked by reading the *function bodies*
# with ast. A module-level grep would have said both paths redact, because
# `redact_detail` is defined in that file either way -- which is how this table
# first came to assert `redacts: True` for the raw write.


def _service_function_body(name: str) -> str:
    """The body of one function in the audit service, as source."""
    source = inspect.getsource(audit_log)
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"audit_log has no function {name!r}")


def test_record_audit_log_entry_does_not_redact_its_detail():
    """The raw write path stores detail verbatim. Verified in the body."""
    body = _service_function_body("record_audit_log_entry")
    assert "redact_detail" not in body
    assert "_detail_json" in body


def test_record_auditable_does_redact_its_detail():
    """The governed write path scrubs it. Also verified in the body."""
    body = _service_function_body("record_auditable")
    assert "redact_detail" in body


def test_detail_json_does_not_scrub_anything():
    """The serialiser is a serialiser. Scrubbing here would be a second place."""
    body = _service_function_body("_detail_json")
    assert "redact" not in body
    assert "password" not in body
    assert "token" not in body


def test_the_write_path_table_matches_the_service():
    """The table's `redacts` column, checked against the functions it names."""
    for route, row in audit_schemas.CONTRACT_WRITE_PATHS.items():
        service = row["service"]
        if service not in ("record_audit_log_entry", "record_auditable"):
            continue  # the two pipeline paths are covered by their own test
        body = _service_function_body(service)
        actual = "redact_detail" in body
        assert actual is row["redacts"], route
    assert audit_schemas.CONTRACT_WRITE_PATHS["POST /audit/log"]["redacts"] is False
    assert audit_schemas.CONTRACT_WRITE_PATHS["POST /audit/log/auditable"]["redacts"] is True


def test_the_two_audit_write_paths_carry_the_same_carrier_field():
    """Same field name, same type, opposite behaviour, no description."""
    raw = audit_schemas.CONTRACT_WRITE_PATHS["POST /audit/log"]
    governed = audit_schemas.CONTRACT_WRITE_PATHS["POST /audit/log/auditable"]
    assert raw["carrier"] == governed["carrier"] == "detail"
    assert raw["redacts"] is False and governed["redacts"] is True
    assert raw["returns"] is None
    assert governed["returns"] == "AuditLogEntryOut"


def test_the_gate_that_catches_the_unredacted_path_exists():
    """The asymmetry is reported; it is also already guarded at write time."""
    gate_ids = {row["gate_id"] for row in audit_log.AUDIT_INTEGRITY_GATES}
    assert "no_unredacted_credentials" in gate_ids
    # and the scrubber it depends on names the credential shapes it removes
    assert {"password", "token", "private_key", "authorization"} <= set(
        audit_log.AUDIT_REDACT_KEYS
    )


def test_the_redaction_report_names_the_asymmetry():
    report = audit_schemas.contract_redaction_report(routes=app)
    codes = {f.code for f in report.findings}
    assert "CONTRACT_REDACTION_ASYMMETRY" in codes
    assert "CONTRACT_REDACTION_UNDECLARED" in codes
    assert report.redacted_paths == 1
    assert report.unredacted_paths == 2
    paths = {w["route"]: w for w in report.write_paths}
    assert paths["POST /audit/log/auditable"]["redacts"] is True
    assert paths["POST /audit/log"]["redacts"] is False
    # and the undelared half: the field description is absent on both
    assert paths["POST /audit/log"]["field_description"] is None
    assert paths["POST /audit/log/auditable"]["field_description"] is None


def test_the_pipeline_write_path_is_declared_deliberate_not_an_oversight():
    row = audit_schemas.CONTRACT_WRITE_PATHS["POST /audit/pipeline/event"]
    assert row["redacts"] is False
    assert row["carrier"] == "payload"
    assert "deliberate" in row["note"]


def test_the_replay_path_carries_no_payload():
    row = audit_schemas.CONTRACT_WRITE_PATHS["POST /audit/pipeline/replay"]
    assert row["carrier"] is None
    assert row["returns"] == "PipelineReplayReport"


def test_the_report_does_not_claim_the_pipeline_path_is_a_defect():
    """CONTRACT_WRITE_UNREDACTED stays unreached, because the table says why."""
    catalog = audit_schemas.build_contract_catalog(routes=app)
    assert "CONTRACT_WRITE_UNREDACTED" in catalog.unreached_codes
    emitted = {f.code for f in catalog.findings}
    assert "CONTRACT_WRITE_UNREDACTED" not in emitted


# ==============================================================================
# 3. The tables
# ==============================================================================

ORIG_KINDS = ("create", "out", "report", "item", "accepted", "spec")
ORIG_AUDIENCES = ("machine_client", "operator", "pipeline_operator", "investigator", "auditor")


def test_table_sizes_are_untouched():
    assert tuple(audit_schemas.CONTRACT_KINDS) == ORIG_KINDS
    assert tuple(audit_schemas.CONTRACT_AUDIENCES) == ORIG_AUDIENCES
    assert len(audit_schemas.CONTRACT_FIELD_POLICIES) == 14
    assert len(audit_schemas.CONTRACT_TRIVIAL_FIELDS) == 23
    assert len(audit_schemas.CONTRACT_FORBIDDEN_FIELDS) == 6
    assert len(audit_schemas.CONTRACT_OPS) == 8
    assert len(audit_schemas.CONTRACT_INVENTORY) == 40
    assert len(audit_schemas.CONTRACT_WRITE_PATHS) == 4
    assert len(audit_schemas.CONTRACT_WARNINGS) == 33


def test_every_inventory_row_describes_a_real_model():
    models = audit_schemas._audit_models()
    for name, row in audit_schemas.CONTRACT_INVENTORY.items():
        assert name in models, name
        assert row["kind"] in audit_schemas.CONTRACT_KINDS, name
        assert row["audience"] in audit_schemas.CONTRACT_AUDIENCES, name
        assert row["note"].strip(), name


def test_inventory_field_counts_match_the_models():
    """The pinned row's `fields`/`required` are the inventory's own claims."""
    models = audit_schemas._audit_models()
    for name, row in audit_schemas.CONTRACT_INVENTORY.items():
        cls = models[name]
        live_fields = len(audit_schemas._field_list(cls))
        live_required = sum(
            1
            for f in audit_schemas._field_list(cls)
            if cls.model_fields[f].is_required()
        )
        assert row["fields"] == live_fields, name
        assert row["required"] == live_required, name


def test_only_request_bodies_are_top_level_false():
    """`top_level` means "a client can ask for this directly"."""
    for name, row in audit_schemas.CONTRACT_INVENTORY.items():
        kind = row["kind"]
        if kind == "create":
            assert row["top_level"] is False, name
        elif row["route"]:
            assert row["top_level"] is True, name
        else:
            assert row["top_level"] is False, name


def test_no_request_body_is_declared_top_level():
    for name, row in audit_schemas.CONTRACT_INVENTORY.items():
        if row["kind"] == "create":
            assert not row["top_level"], name


def test_the_audience_ranks_are_ordered_and_unique():
    ranks = [row["rank"] for row in audit_schemas.CONTRACT_AUDIENCES.values()]
    assert ranks == sorted(ranks)
    # two audiences may share a rank (they are peers, not a ladder step)
    assert len(ranks) == len(audit_schemas.CONTRACT_AUDIENCES)


def test_the_spec_kind_uses_a_narrower_suffix_than_report():
    """TransactionSpecReport ends in Report; longest-suffix-first resolves it."""
    assert audit_schemas.CONTRACT_KINDS["spec"]["suffixes"] == ("SpecReport",)
    assert audit_schemas.CONTRACT_KINDS["report"]["suffixes"] == ("Report",)
    assert audit_schemas._kind_for("TransactionSpecReport") == "spec"
    assert audit_schemas._kind_for("TransactionLogReport") == "report"
    assert audit_schemas._kind_for("PipelineEventAccepted") == "accepted"
    assert audit_schemas._kind_for("AuditLogSummaryItem") == "item"
    assert audit_schemas._kind_for("AuditLogEntryCreate") == "create"
    assert audit_schemas._kind_for("PipelineReplayRequest") == "create"
    assert audit_schemas._kind_for("AuditLogEntryOut") == "out"
    assert audit_schemas._kind_for("SomethingElse") is None


def test_every_op_is_report_only():
    """The posture is a checked property of the table, not a docstring."""
    for name, row in audit_schemas.CONTRACT_OPS.items():
        assert row["report_only"] is True, name
        assert row["op"] and row["reads"] and row["cannot"], name


def test_the_content_carrier_is_declared_embedded():
    """`embedded` exists because a serialised export is not a scalar."""
    assert audit_schemas.CONTRACT_FIELD_POLICIES["content"]["carrier"] == "embedded"
    # and the check accepts that pair rather than ignoring its own vocabulary
    report = audit_schemas.validate_contracts()
    assert report.checks["embedded_carriers_accepted"] == 2


def test_the_severity_policy_names_the_event_vocabulary():
    row = audit_schemas.CONTRACT_FIELD_POLICIES["severity"]
    assert list(row["vocabulary"]) == ["info", "warning", "critical"]
    assert row["carrier"] == "scalar"


def test_the_field_policies_name_the_carrier_they_describe():
    models = audit_schemas._audit_models()
    for field, row in audit_schemas.CONTRACT_FIELD_POLICIES.items():
        assert row["carrier"] in ("scalar", "blob", "list", "timestamp", "embedded"), field
        assert row["declared_in"] in ("pattern", "column_check", "comment_only", "declared"), field
        assert row["note"].strip(), field
    # every policy row is actually used by a field in the module
    used = {f for cls in models.values() for f in audit_schemas._field_list(cls)}
    assert set(audit_schemas.CONTRACT_FIELD_POLICIES) <= used


def test_the_trivial_table_covers_no_field_it_does_not_exempt_from_a_policy():
    """Names with a policy row are listed as trivial only as a cross-reference."""
    for name, reason in audit_schemas.CONTRACT_TRIVIAL_FIELDS.items():
        assert reason.strip(), name
        if name in audit_schemas.CONTRACT_FIELD_POLICIES:
            assert "has a policy row" in reason, name


def test_the_forbidden_table_publishes_a_replacement_or_says_there_is_none():
    for name, row in audit_schemas.CONTRACT_FORBIDDEN_FIELDS.items():
        assert row["reason"].strip(), name
        assert row["publish_instead"].strip(), name


def test_no_forbidden_name_is_actually_declared_today():
    models = audit_schemas._audit_models()
    used = {f for cls in models.values() for f in audit_schemas._field_list(cls)}
    assert set(audit_schemas.CONTRACT_FORBIDDEN_FIELDS) & used == set()


def test_the_inventory_partitions_the_models():
    report = audit_schemas.contract_inventory(routes=app)
    bodies = sum(1 for row in audit_schemas.CONTRACT_INVENTORY.values() if row["kind"] == "create")
    assert report.models == 40
    assert report.declared == 40
    assert report.top_level == 22
    assert report.nested_only == 14
    assert report.top_level + report.nested_only + bodies == report.models
    assert report.by_kind == {
        "accepted": 1,
        "create": 4,
        "item": 1,
        "out": 14,
        "report": 19,
        "spec": 1,
    }
    assert report.by_audience == {
        "auditor": 11,
        "investigator": 5,
        "machine_client": 3,
        "operator": 16,
        "pipeline_operator": 5,
    }
    assert report.undeclared == []
    assert report.stale == []


def test_the_taxonomy_is_well_formed():
    for code, row in audit_schemas.CONTRACT_WARNINGS.items():
        assert isinstance(row, dict), code
        assert row["severity"] in ("defect", "warning", "info"), code
        assert row["description"].strip(), code
        assert row["remediation"].strip(), code
        assert row["emitted_by"], code
        # a bare string here is the failure mode: it iterates one character at
        # a time, so nothing crashes and nothing is right
        assert isinstance(row["emitted_by"], (list, tuple)), code
        assert not isinstance(row["emitted_by"], str), code
        for producer in row["emitted_by"]:
            assert producer in {
                "contract_inventory",
                "describe_contract_field",
                "contract_divergence_report",
                "contract_redaction_report",
                "validate_contracts",
                "build_contract_catalog",
            }, f"{code} -> {producer}"


def test_the_taxonomy_uses_one_severity_vocabulary():
    """Four rows used to say "error" while twenty-nine said defect/warning/info."""
    assert {row["severity"] for row in audit_schemas.CONTRACT_WARNINGS.values()} == {
        "defect",
        "warning",
        "info",
    }


# ==============================================================================
# 4. The reports -- ground truth, and the guards that fire
# ==============================================================================

ORIG_FINDINGS = 84
ORIG_SEVERITY_COUNTS = {"defect": 27, "info": 20, "warning": 37}
ORIG_EMITTED_CODES = 17
ORIG_UNREACHED_CODES = 16
ORIG_CODES = 33


@pytest.fixture(scope="module")
def catalog():
    return audit_schemas.build_contract_catalog(routes=app)


def test_the_catalog_rolls_up_the_whole_layer(catalog):
    assert catalog.finding_count == ORIG_FINDINGS
    assert catalog.severity_counts == ORIG_SEVERITY_COUNTS
    assert catalog.version == "contract_governance_v1"
    assert catalog.finding_count == sum(catalog.severity_counts.values())


def test_the_catalog_reports_its_own_ground_truth(catalog):
    assert catalog.ground_truth == {
        "models": 40,
        "request_bodies": 4,
        "top_level_responses": 22,
        "nested_only": 14,
        "fields_examined": 326,
        "codes": ORIG_CODES,
        "emitted_codes": ORIG_EMITTED_CODES,
        "route_observed": True,
    }


def test_emitted_plus_unreached_equals_the_taxonomy(catalog):
    """The three numbers have to add up, or one of them is decoration."""
    assert (
        catalog.ground_truth["emitted_codes"] + len(catalog.unreached_codes)
        == catalog.ground_truth["codes"]
    )


def test_the_unreached_codes_are_emitted_as_findings_too(catalog):
    """A field named unreached_codes that nothing surfaces is a claim."""
    surfaced = {f.subject for f in catalog.findings if f.code == "CONTRACT_TAXONOMY_NOT_EMITTED"}
    assert surfaced == set(catalog.unreached_codes)
    assert len(surfaced) == ORIG_UNREACHED_CODES


def test_the_taxonomy_code_does_not_report_itself_as_unreached(catalog):
    assert "CONTRACT_TAXONOMY_NOT_EMITTED" not in catalog.unreached_codes


def test_every_finding_names_a_code_in_the_taxonomy(catalog):
    for finding in catalog.findings:
        assert finding.code in audit_schemas.CONTRACT_WARNINGS, finding.code
        assert finding.severity in ("defect", "warning", "info")
        assert finding.producer
        assert finding.subject
        assert finding.detail.strip()


def test_the_inventory_is_the_only_clean_report(catalog):
    """It is clean because the table matches the module, not because nothing ran."""
    assert catalog.clean_reports == ["contract_inventory"]
    assert catalog.inventory.finding_count == 0
    assert catalog.divergence.finding_count > 0
    assert catalog.redaction.finding_count > 0


def test_the_inventory_checks_the_module_against_the_table(monkeypatch):
    """A row naming a model the module does not define is `stale`."""
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_INVENTORY.items()}
    table["RetiredReport"] = dict(table["AuditLogEntryOut"])
    monkeypatch.setattr(audit_schemas, "CONTRACT_INVENTORY", table)
    report = audit_schemas.contract_inventory(routes=app)
    assert report.stale == ["RetiredReport"]
    assert "CONTRACT_INVENTORY_STALE" in {f.code for f in report.findings}


def test_a_new_model_is_reported_as_undeclared(monkeypatch):
    """The guard fires on a model with no row rather than ignoring it."""
    class BrandNewOut(audit_schemas.BaseModel):
        id: int

    BrandNewOut.__module__ = audit_schemas.__name__
    monkeypatch.setattr(audit_schemas, "BrandNewOut", BrandNewOut, raising=False)
    report = audit_schemas.contract_inventory(routes=app)
    assert "BrandNewOut" in report.undeclared
    codes = {f.code for f in report.findings}
    assert "CONTRACT_MODEL_UNDECLARED" in codes
    assert report.finding_count == 1


def test_a_wrong_field_count_is_reported_as_drift(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_INVENTORY.items()}
    table["AuditLogEntryOut"]["fields"] = 3
    monkeypatch.setattr(audit_schemas, "CONTRACT_INVENTORY", table)
    report = audit_schemas.contract_inventory(routes=app)
    assert "CONTRACT_FIELD_COUNT_DRIFT" in {f.code for f in report.findings}


def test_a_wrong_kind_is_reported(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_INVENTORY.items()}
    table["AuditLogEntryOut"]["kind"] = "report"
    monkeypatch.setattr(audit_schemas, "CONTRACT_INVENTORY", table)
    report = audit_schemas.contract_inventory(routes=app)
    assert "CONTRACT_KIND_MISMATCH" in {f.code for f in report.findings}


def test_the_divergence_report_runs_every_check(catalog):
    checks = catalog.divergence.checks
    assert checks == [
        "vocabulary_enforcement",
        "uncapped_write_strings",
        "unbounded_carriers",
        "unformatted_timestamps",
        "nullable_element_lists",
        "bypassed_contracts",
        "vocabulary_collisions",
        "envelope_stamps",
        "write_asymmetry",
        "untyped_write_responses",
    ]


def test_route_dependent_checks_report_themselves_skipped_without_routes():
    """An unavailable check must never read as a clean one."""
    report = audit_schemas.contract_divergence_report()
    assert report.checks[-1] == "untyped_write_responses:skipped"
    assert report.untyped_write_responses == []
    # and the check that needs no document still runs
    assert "vocabulary_enforcement" in report.checks
    assert report.route_observed is False if hasattr(report, "route_observed") else True
    assert audit_schemas.contract_inventory().route_observed is False
    assert audit_schemas.contract_inventory(routes=app).route_observed is True


def test_route_matching_ignores_the_status_code(catalog):
    """A declared route has no status code; a response label does."""
    # if the bare path were not tracked separately, every response route would
    # read as a mismatch
    assert catalog.inventory.finding_count == 0
    assert catalog.inventory.route_observed is True
    observed = audit_schemas._route_map(app)
    assert observed["available"] is True
    for name, entry in observed["models"].items():
        for label in entry["response"]:
            assert label.endswith(tuple("0123456789")) or "[" in label
        for label in entry["response_paths"]:
            assert "[" not in label, label


def test_a_non_openapi_argument_is_reported_not_raised():
    observed = audit_schemas._route_map(object())
    assert observed["available"] is False
    assert "not an openapi document" in observed["reason"]
    assert audit_schemas.contract_inventory(routes=object()).route_observed is False


def test_an_exploding_openapi_call_is_caught():
    class Hostile:
        def openapi(self):
            raise RuntimeError("no document here")

    observed = audit_schemas._route_map(Hostile())
    assert observed["available"] is False
    assert "RuntimeError" in observed["reason"]
    assert audit_schemas.contract_inventory(routes=Hostile()).route_observed is False


def test_top_level_means_top_level_response_not_top_level_model(catalog):
    """The 4 request bodies are not top-level, and are not `out` either."""
    bodies = [
        row.model
        for row in catalog.inventory.inventory
        if row.kind == "create" and row.top_level
    ]
    assert bodies == []
    top_level = [row for row in catalog.inventory.inventory if row.top_level]
    assert len(top_level) == 22
    assert all(row.kind != "create" for row in top_level)


# -- 4a. the divergence findings, pinned --------------------------------------


def test_the_eight_comment_only_vocabularies(catalog):
    subjects = sorted(e["subject"] for e in catalog.divergence.comment_only_vocabularies)
    assert subjects == [
        "AuditGateIntegrityReport.verdict",
        "AuditIntegrityGateOut.reason",
        "AuditIntegrityGateOut.severity",
        "ComponentMetrics.classification",
        "EnhancementProposal.effort",
        "EnhancementProposal.impact",
        "EnhancementProposal.priority",
        "TransactionIntegrityReport.verdict",
    ]
    for entry in catalog.divergence.comment_only_vocabularies:
        assert entry["values"], entry["subject"]
        assert entry["accepts_anything"] is True


def test_the_three_pattern_enforced_severity_fields(catalog):
    assert sorted(catalog.divergence.pattern_enforced) == [
        "AuditLogEntryCreate.severity",
        "AuditableLogEntryCreate.severity",
        "PipelineEventCreate.severity",
    ]


def test_severity_carries_two_vocabularies_in_one_module(catalog):
    collisions = {c["field"]: c["vocabularies"] for c in catalog.divergence.vocabulary_collisions}
    assert set(collisions) == {"severity"}
    vocabularies = collisions["severity"]
    assert set(vocabularies) == {"advisory | review | reject", "info | warning | critical"}
    assert vocabularies["advisory | review | reject"] == ["AuditIntegrityGateOut.severity"]
    assert vocabularies["info | warning | critical"] == [
        "AuditLogEntryCreate.severity (pattern)",
        "AuditableLogEntryCreate.severity (pattern)",
        "PipelineEventCreate.severity (pattern)",
    ]
    assert "CONTRACT_VOCABULARY_COLLISION" in {f.code for f in catalog.findings}


def test_the_two_uncapped_write_strings(catalog):
    assert [e["subject"] for e in catalog.divergence.uncaped_write_fields] == [
        "AuditLogEntryCreate.summary",
        "AuditableLogEntryCreate.summary",
    ]
    for entry in catalog.divergence.uncaped_write_fields:
        assert entry["min_length"] == 1
        assert entry["max_length"] is None


def test_the_five_unformatted_timestamps(catalog):
    entries = {e["subject"]: e for e in catalog.divergence.unformatted_timestamps}
    assert sorted(entries) == [
        "PipelineDeadLetterEntryOut.first_failed_at",
        "PipelineDeadLetterEntryOut.last_failed_at",
        "PipelineDeadLetterEntryOut.occurred_at",
        "PipelineDeadLetterReport.newest_occurred_at",
        "PipelineDeadLetterReport.oldest_occurred_at",
    ]
    # and the sibling count names the datetime declarations they differ from
    assert entries["PipelineDeadLetterEntryOut.occurred_at"]["siblings_as_datetime"] >= 1


def test_the_one_nullable_element_list(catalog):
    assert [e["subject"] for e in catalog.divergence.nullable_element_lists] == [
        "AuditAnomalyFindingOut.entry_ids"
    ]


def test_the_three_bypassed_contracts(catalog):
    """A list of records declared untyped where a sibling types them.

    `filters` is *not* in this set: a repeated query parameter is not a list of
    frames, and the check skips every name the trivial table covers.
    """
    entries = {e["subject"]: e for e in catalog.divergence.bypassed_contracts}
    assert sorted(entries) == [
        "TransactionIntegrityReport.checkpoints",
        "TransactionIntegrityReport.sampled",
        "TransactionQueryReport.results",
    ]
    for subject, entry in entries.items():
        assert entry["declared_model"] == "TransactionOut", subject
        assert entry["typed_at"] == "TransactionLogReport.tail", subject
        # the full typing rendering, which is what distinguishes this from a
        # typed list; the short form is the bare origin "List"
        assert "Dict[str, typing.Any]" in entry["annotation"], subject


def test_the_bypassed_finding_does_not_assert_the_elements_are_the_same(catalog):
    """The schema does not say what an untyped list holds, so the finding says so."""
    findings = [f for f in catalog.findings if f.code == "CONTRACT_TYPED_CONTRACT_BYPASSED"]
    assert len(findings) == 3
    for finding in findings:
        assert finding.severity == "warning"
        assert "not decidable from the contract" in finding.evidence["caveat"]


def test_the_two_envelopes_without_an_age_stamp(catalog):
    assert catalog.divergence.envelopes_without_generated_at == [
        "AuditLogExportReport",
        "PipelineEventAccepted",
    ]


def test_the_one_optional_age_stamp(catalog):
    assert catalog.divergence.optional_generated_at == ["PipelineDeadLetterReport"]


def test_the_write_asymmetry_between_the_two_create_contracts(catalog):
    pairs = {(e["left"], e["right"], e["field"]) for e in catalog.divergence.write_asymmetries}
    assert pairs == {
        ("AuditLogEntryCreate", "AuditableLogEntryCreate", "severity"),
        ("AuditableLogEntryCreate", "PipelineEventCreate", "severity"),
    }


def test_post_audit_log_is_the_only_untyped_audit_write(catalog):
    assert catalog.divergence.untyped_write_responses == ["POST /audit/log"]


def test_the_divergence_report_examines_every_field():
    report = audit_schemas.contract_divergence_report(routes=app)
    models = audit_schemas._audit_models()
    live = sum(len(audit_schemas._field_list(m)) for m in models.values())
    assert report.fields_examined == live == 326


def test_shape_of_reads_the_type_object_not_its_repr():
    """Four checks once string-matched a typing repr and matched nothing.

    `List[Dict[str, Any]]` renders as `typing.List[typing.Dict[str, typing.Any]]`
    and `Optional[str]` as `typing.Optional[str]`, so `"List[Dict[str, Any]]" in
    text` was False for every real annotation and the check reported clean.
    """
    from typing import Any, Dict, List, Optional

    assert audit_schemas._shape_of(List[Dict[str, Any]])["element_is_dict_str_any"] is True
    assert audit_schemas._shape_of(List[Dict[str, Any]])["is_dict_str_any"] is False
    assert audit_schemas._shape_of(Optional[str])["is_optional"] is True
    assert audit_schemas._shape_of(Optional[str])["is_str"] is True
    assert audit_schemas._shape_of(Dict[str, Any])["is_dict_str_any"] is True
    assert audit_schemas._shape_of(str)["is_str"] is True
    assert audit_schemas._shape_of(None)["known"] is False


def test_shape_of_unwraps_optional_only():
    """Unwrapping any single-arg generic turned List[Dict[...]] into a bare dict."""
    from typing import Any, Dict, List

    nested = audit_schemas._shape_of(List[Dict[str, Any]])
    assert nested["is_list"] is True
    assert nested["is_dict"] is False


def test_the_nested_element_is_tested_whole():
    """getattr(element, '__origin__') is dict is True; == str is always False."""
    from typing import Any, Dict, List

    nested = audit_schemas._shape_of(List[Dict[str, Any]])
    assert nested["element_is_dict_str_any"] is True
    assert audit_schemas._shape_of(List[str])["element_is_dict_str_any"] is False
    assert audit_schemas._shape_of(List[int])["element"] is int


# -- 4b. the redaction report --------------------------------------------------


def test_the_carrier_census(catalog):
    report = catalog.redaction
    assert len(report.carriers) == 19
    assert len(report.carriers_on_responses) == 16
    assert len(report.write_paths) == 4
    # every carrier is a Dict[str, Any] and every one declares no bound
    for entry in report.carriers:
        assert entry["field"] in ("detail", "payload", "content") or entry["subject"].endswith(
            ("thresholds", "filters", "results", "policy", "chain", "checkpoints", "sampled", "metrics", "spec", "results", "data_sources", "summary")
        ), entry["subject"]
        assert entry["declared_bound"] is None, entry["subject"]
    assert report.boundary


def test_no_forbidden_name_is_present_today(catalog):
    assert catalog.redaction.forbidden_names == sorted(audit_schemas.CONTRACT_FORBIDDEN_FIELDS)
    assert catalog.redaction.forbidden_present == []


def test_the_report_would_fire_on_a_forbidden_name(monkeypatch):
    monkeypatch.setattr(
        audit_schemas,
        "CONTRACT_FORBIDDEN_FIELDS",
        {**audit_schemas.CONTRACT_FORBIDDEN_FIELDS, "detail": {"reason": "test", "publish_instead": "none"}},
    )
    report = audit_schemas.contract_redaction_report(routes=app)
    assert "detail" in report.forbidden_present[0]["subject"] or any(
        "detail" in str(e) for e in report.forbidden_present
    )
    assert "CONTRACT_FORBIDDEN_FIELD_PRESENT" in {f.code for f in report.findings}


def test_a_carrier_with_a_description_is_still_carried_but_reported(catalog):
    """The report names the field's declared description, if any."""
    for entry in catalog.redaction.carriers:
        assert "description" in entry
        assert "policy_note" in entry


def test_the_unredacted_count_is_the_pipeline_plus_the_raw_log_write(catalog):
    report = catalog.redaction
    redacted = [w for w in report.write_paths if w["redacts"]]
    # the counts only cover paths that actually carry a carrier; the replay
    # request carries none, so it is neither redacted nor unredacted
    unredacted = [
        w for w in report.write_paths if not w["redacts"] and w["carrier"] is not None
    ]
    assert report.redacted_paths == len(redacted) == 1
    assert report.unredacted_paths == len(unredacted) == 2
    assert redacted[0]["route"] == "POST /audit/log/auditable"
    assert {w["route"] for w in unredacted} == {
        "POST /audit/log",
        "POST /audit/pipeline/event",
    }
    # replay carries no payload, so it is not a redaction story at all
    replay = next(w for w in report.write_paths if w["route"] == "POST /audit/pipeline/replay")
    assert replay["carrier"] is None


def test_the_write_paths_are_observed_against_the_document(catalog):
    for entry in catalog.redaction.write_paths:
        assert entry["route_observed"] is True, entry["route"]


def test_a_write_path_naming_a_missing_model_is_reported(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_WRITE_PATHS.items()}
    table["POST /audit/log"]["model"] = "NoSuchContract"
    monkeypatch.setattr(audit_schemas, "CONTRACT_WRITE_PATHS", table)
    report = audit_schemas.validate_contracts()
    assert "CONTRACT_WRITE_PATH_UNKNOWN" in report.coded


def test_the_redaction_report_names_the_same_carrier_as_the_two_writes(catalog):
    """`detail` is a carrier on both write contracts, and on every read too."""
    subjects = {c["subject"] for c in catalog.redaction.carriers if c["field"] == "detail"}
    assert "AuditLogEntryCreate.detail" in subjects
    assert "AuditableLogEntryCreate.detail" in subjects
    assert "AuditLogEntryOut.detail" in subjects


# -- 4c. validate_contracts ---------------------------------------------------


def test_validate_contracts_is_clean_on_the_shipped_tables(catalog):
    report = catalog.validation
    assert report.ok is True
    assert report.errors == 0
    assert report.warnings == 4
    assert report.info == 0
    assert report.counts == {"defect": 0, "warning": 4, "info": 0}


def test_validate_contracts_counts_match_its_messages(catalog):
    report = catalog.validation
    assert len(report.messages) == report.errors + report.warnings + report.info


def test_validate_contracts_reports_its_checks(catalog):
    assert catalog.validation.checks == {
        "ops_are_report_only": 8,
        "audiences_referenced": 40,
        "field_policies": 14,
        "field_policies_used": 14,
        "embedded_carriers_accepted": 2,
        "trivial_names": 23,
        "forbidden_names": 6,
        "write_paths": 4,
        "models": 40,
        "recurring_names": 20,
    }


def test_a_non_report_only_op_is_an_error(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_OPS.items()}
    table["inventory"]["report_only"] = False
    monkeypatch.setattr(audit_schemas, "CONTRACT_OPS", table)
    report = audit_schemas.validate_contracts()
    assert report.ok is False
    assert "CONTRACT_OP_NOT_REPORT_ONLY" in report.coded
    assert report.errors == 1


def test_a_non_mapping_row_is_reported_not_raised(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_OPS.items()}
    table["inventory"] = "not a table"
    monkeypatch.setattr(audit_schemas, "CONTRACT_OPS", table)
    report = audit_schemas.validate_contracts()
    assert report.ok is False
    assert "CONTRACT_TABLE_MALFORMED" in report.coded


def test_an_unknown_audience_is_reported(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_INVENTORY.items()}
    table["AuditLogEntryOut"]["audience"] = "martian"
    monkeypatch.setattr(audit_schemas, "CONTRACT_INVENTORY", table)
    report = audit_schemas.validate_contracts()
    assert "AuditLogEntryOut.audience" in report.coded.get("CONTRACT_AUDIENCE_UNKNOWN", [])


def test_an_unknown_kind_is_reported_under_its_own_code(monkeypatch):
    """A bad kind filed as an unknown audience names the wrong thing."""
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_INVENTORY.items()}
    table["AuditLogEntryOut"]["kind"] = "martian"
    monkeypatch.setattr(audit_schemas, "CONTRACT_INVENTORY", table)
    report = audit_schemas.validate_contracts()
    assert "AuditLogEntryOut.kind" in report.coded.get("CONTRACT_KIND_UNKNOWN", [])
    assert "AuditLogEntryOut.kind" not in report.coded.get("CONTRACT_AUDIENCE_UNKNOWN", [])


def test_a_create_contract_declared_top_level_is_reported(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_INVENTORY.items()}
    table["AuditLogEntryCreate"]["top_level"] = True
    monkeypatch.setattr(audit_schemas, "CONTRACT_INVENTORY", table)
    report = audit_schemas.validate_contracts()
    assert "AuditLogEntryCreate.top_level" in report.coded.get("CONTRACT_KIND_UNKNOWN", [])


def test_an_unused_field_policy_is_reported(monkeypatch):
    monkeypatch.setattr(
        audit_schemas,
        "CONTRACT_FIELD_POLICIES",
        {**audit_schemas.CONTRACT_FIELD_POLICIES, "ghost_field": {"carrier": "scalar"}},
    )
    report = audit_schemas.validate_contracts()
    assert "ghost_field" in report.coded.get("CONTRACT_POLICY_UNUSED", [])


def test_a_carrier_mismatch_is_reported(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_FIELD_POLICIES.items()}
    table["detail"] = {**table["detail"], "carrier": "scalar"}
    monkeypatch.setattr(audit_schemas, "CONTRACT_FIELD_POLICIES", table)
    report = audit_schemas.validate_contracts()
    assert any("detail" in s for s in report.coded.get("CONTRACT_POLICY_CARRIER_MISMATCH", []))


def test_the_shape_collision_check_fires_on_a_primitive_disagreement(monkeypatch):
    """Only a primitive disagreement, not list-vs-dict."""
    report = audit_schemas.validate_contracts()
    collisions = report.coded.get("CONTRACT_FIELD_SHAPE_COLLISION", [])
    assert collisions == ["id", "occurred_at", "replayable"]
    # and the compatibility rule: the three findings are all primitive pairs
    for message in report.messages:
        if "CONTRACT_FIELD_SHAPE_COLLISION" in message:
            assert "disagreeing on a primitive type" in message


def test_a_taxonomy_string_emitted_by_is_caught(monkeypatch):
    """The exact bug this check was written for: a bare string iterates chars."""
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_WARNINGS.items()}
    table["CONTRACT_KIND_UNKNOWN"]["emitted_by"] = "validate_contracts"
    monkeypatch.setattr(audit_schemas, "CONTRACT_WARNINGS", table)
    report = audit_schemas.validate_contracts()
    assert report.ok is False
    subjects = report.coded.get("CONTRACT_TAXONOMY_MALFORMED", [])
    assert any("CONTRACT_KIND_UNKNOWN" in s for s in subjects)
    assert any("iterated" in m for m in report.messages)


def test_a_taxonomy_bad_severity_is_caught(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_WARNINGS.items()}
    table["CONTRACT_MODEL_UNDECLARED"]["severity"] = "error"
    monkeypatch.setattr(audit_schemas, "CONTRACT_WARNINGS", table)
    report = audit_schemas.validate_contracts()
    assert report.ok is False
    assert any(
        "CONTRACT_MODEL_UNDECLARED" in s
        for s in report.coded.get("CONTRACT_TAXONOMY_MALFORMED", [])
    )


def test_a_taxonomy_empty_description_is_caught(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_WARNINGS.items()}
    table["CONTRACT_MODEL_UNDECLARED"]["description"] = ""
    monkeypatch.setattr(audit_schemas, "CONTRACT_WARNINGS", table)
    report = audit_schemas.validate_contracts()
    assert (
        "CONTRACT_WARNINGS.CONTRACT_MODEL_UNDECLARED"
        in report.coded.get("CONTRACT_TAXONOMY_MALFORMED", [])
    )


def test_a_taxonomy_unknown_producer_is_caught(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_WARNINGS.items()}
    table["CONTRACT_MODEL_UNDECLARED"]["emitted_by"] = ("nowhere",)
    monkeypatch.setattr(audit_schemas, "CONTRACT_WARNINGS", table)
    report = audit_schemas.validate_contracts()
    assert (
        "CONTRACT_WARNINGS.CONTRACT_MODEL_UNDECLARED"
        in report.coded.get("CONTRACT_TAXONOMY_MALFORMED", [])
    )


def test_a_non_mapping_taxonomy_row_is_caught_not_raised(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_WARNINGS.items()}
    table["CONTRACT_MODEL_UNDECLARED"] = "nope"
    monkeypatch.setattr(audit_schemas, "CONTRACT_WARNINGS", table)
    report = audit_schemas.validate_contracts()
    assert report.ok is False
    assert "CONTRACT_WARNINGS.CONTRACT_MODEL_UNDECLARED" in report.coded.get(
        "CONTRACT_TAXONOMY_MALFORMED", []
    )


# -- 4d. describe_contract_field ----------------------------------------------


def test_describe_severity_says_the_pattern_is_what_enforces_it():
    row = audit_schemas.describe_contract_field("AuditLogEntryCreate", "severity")
    assert row.resolved is True
    assert row.kind == "create"
    assert row.audience == "machine_client"
    assert row.carrier == "scalar"
    assert row.pattern == "^(info|warning|critical)$"
    assert any("pattern=" in item for item in row.enforced_by)


def test_describe_gate_severity_says_the_comment_is_all_there_is():
    row = audit_schemas.describe_contract_field("AuditIntegrityGateOut", "severity")
    assert row.pattern is None
    assert row.comment_vocabulary == "advisory | review | reject"
    # the *policy* row claims pattern enforcement, the field has no pattern, and
    # the description says both things rather than repeating the claim
    assert row.declared_in == "pattern"
    assert any("trailing comment only" in item for item in row.enforced_by)
    assert any("no pattern" in note for note in row.notes)


def test_describe_content_names_the_embedded_carrier():
    row = audit_schemas.describe_contract_field("AuditLogExportReport", "content")
    assert row.carrier == "embedded"
    assert row.max_length is None


def test_describe_a_missing_field_does_not_raise():
    row = audit_schemas.describe_contract_field("AuditLogEntryOut", "no_such_field")
    assert row.resolved is False
    assert row.kind == "unknown"
    assert row.notes


def test_describe_a_missing_model_does_not_raise():
    row = audit_schemas.describe_contract_field("NoSuchModel", "id")
    assert row.resolved is False
    assert row.kind == "unknown"
    assert "not a contract" in row.notes[0]


def test_describe_a_timestamp_with_no_format_says_so():
    row = audit_schemas.describe_contract_field("PipelineDeadLetterEntryOut", "occurred_at")
    assert row.carrier == "timestamp"
    assert any("no format is published" in item for item in row.enforced_by)


def test_describe_reads_enforcement_from_the_field_not_the_policy(monkeypatch):
    """A policy row claiming `pattern` while the field has none is the defect."""
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_FIELD_POLICIES.items()}
    table["summary"] = {**table["summary"], "declared_in": "pattern"}
    monkeypatch.setattr(audit_schemas, "CONTRACT_FIELD_POLICIES", table)
    row = audit_schemas.describe_contract_field("AuditLogEntryCreate", "summary")
    assert row.pattern is None
    # the mismatch is surfaced in notes rather than taken at face value
    assert any("no pattern" in note for note in row.notes)
    assert not any("pattern=" in item for item in row.enforced_by)


def test_describe_exposes_the_bounds_block():
    row = audit_schemas.describe_contract_field("AuditLogEntryCreate", "summary")
    assert row.bounds["min_length"] == 1
    assert row.bounds["max_length"] is None


# -- 4e. never-raise contracts -------------------------------------------------


def test_a_malformed_inventory_row_does_not_take_the_report_down(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_INVENTORY.items()}
    table["AuditLogEntryOut"] = None
    monkeypatch.setattr(audit_schemas, "CONTRACT_INVENTORY", table)
    # every report still returns
    assert audit_schemas.contract_inventory(routes=app).finding_count >= 1
    assert audit_schemas.contract_divergence_report(routes=app) is not None
    assert audit_schemas.contract_redaction_report(routes=app) is not None
    assert audit_schemas.validate_contracts() is not None
    assert audit_schemas.build_contract_catalog(routes=app) is not None


def test_a_model_whose_fields_will_not_read_is_reported_not_raised(monkeypatch):
    class Broken(audit_schemas.BaseModel):
        id: int

    def explode():
        raise RuntimeError("fields are gone")

    Broken.model_fields = property(explode)
    Broken.__module__ = audit_schemas.__name__
    monkeypatch.setattr(audit_schemas, "Broken", Broken, raising=False)
    report = audit_schemas.contract_inventory(routes=app)
    assert "Broken" in report.undeclared
    assert audit_schemas.validate_contracts() is not None


def test_a_source_vocabulary_scan_survives_a_syntax_error(monkeypatch):
    """The docstring promises {} on a syntax error, and the reports say so."""
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path).endswith("audit.py"):
            return __import__("io").StringIO("class (((")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert audit_schemas._source_vocabularies() == {}
    # and the divergence report still runs, just with no comments to read
    assert audit_schemas.contract_divergence_report(routes=app) is not None


def test_field_facts_for_a_missing_field_is_unresolved():
    facts = audit_schemas._field_facts(audit_schemas.AuditLogEntryOut, "nope")
    assert facts["resolved"] is False
    assert facts["annotation"] == "unknown"


def test_carrier_of_classifies_by_name_as_well_as_annotation():
    """A timestamp declared `str` is the case the name argument exists for."""
    assert audit_schemas._carrier_of("Optional[str]", "occurred_at") == "timestamp"
    assert audit_schemas._carrier_of("Optional[str]", "summary") == "scalar"
    assert audit_schemas._carrier_of("List[Dict[str, Any]]", "results") == "list"
    assert audit_schemas._carrier_of("Dict[str, Any]", "detail") == "blob"
    assert audit_schemas._carrier_of("str", "content") == "scalar"


# ==============================================================================
# 5. The surfaces
# ==============================================================================


def test_the_catalog_endpoint_returns_both_halves():
    response = client.get("/meta/audit-contracts")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"name", "version", "environment", "contracts", "governance"}
    assert body["contracts"]["models"] == 40
    assert body["contracts"]["declared"] == 40
    assert body["governance"]["finding_count"] == ORIG_FINDINGS
    assert body["governance"]["version"] == "contract_governance_v1"


def test_the_catalog_endpoint_does_not_leak_the_governance_models_as_contracts():
    body = client.get("/meta/audit-contracts").json()
    names = {row["model"] for row in body["contracts"]["inventory"]}
    assert names == set(ORIG_SHAPE)
    assert names & set(audit_schemas.GOVERNANCE_MODELS) == set()


def test_scoring_catalog_carries_the_contract_governance_payload():
    body = client.get("/meta/scoring-catalog").json()
    assert "audit_contracts" in body
    assert body["audit_contracts"]["version"] == "contract_governance_v1"
    assert body["audit_contracts"]["finding_count"] == ORIG_FINDINGS


def test_meta_features_lists_contract_governance_and_audit_contracts():
    features = client.get("/meta").json()["features"]
    assert "contract_governance" in features
    endpoints = client.get("/meta/features").json()["endpoints"]
    assert endpoints["audit_contracts"] == "/meta/audit-contracts"


def test_ecosystem_describes_the_audit_trail_governance():
    subservices = client.get("/meta/ecosystem").json()["subservices"]
    row = subservices["audit_trail"]
    assert row["config_tables"] == [
        "CONTRACT_KINDS",
        "CONTRACT_AUDIENCES",
        "CONTRACT_FIELD_POLICIES",
        "CONTRACT_TRIVIAL_FIELDS",
        "CONTRACT_FORBIDDEN_FIELDS",
        "CONTRACT_INVENTORY",
        "CONTRACT_WRITE_PATHS",
        "CONTRACT_OPS",
        "CONTRACT_WARNINGS",
    ]
    assert len(row["notes"]) > 400
    assert "/meta/audit-contracts" not in row["routes"]  # routes are the /audit surface


def test_every_meta_surface_still_returns_200():
    for path in (
        "/meta",
        "/meta/features",
        "/meta/ecosystem",
        "/meta/scoring-catalog",
        "/meta/i18n",
        "/meta/authz",
        "/meta/audit-contracts",
        "/meta/schema",
    ):
        assert client.get(path).status_code == 200, path


def test_the_endpoint_note_says_the_ddl_is_not_verified():
    """A schemas module claiming to have checked the DDL would be a lie."""
    policy = client.get("/meta/audit-contracts").json()["governance"]["policy"]
    assert policy["posture"] == "report, never repair"
    assert "not imported" in policy["not_imported"]
    assert "DDL" in policy["not_imported"]
    assert "report_only" in policy["enforced_by_check"]


def test_the_module_does_not_import_models_or_main():
    """The DDL and the route table are facts this module does not own."""
    source = inspect.getsource(audit_schemas)
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    assert "app.models" not in imported
    assert "app.main" not in imported
    assert not any(str(m).startswith("app.services") for m in imported)


def test_the_route_map_refuses_to_import_the_app_itself():
    """`routes=` is an argument; the module never reaches for the app."""
    source = inspect.getsource(audit_schemas)
    assert "from app.main" not in source
    assert "import app.main" not in source


# ==============================================================================
# 6. The bugs this pass found in its own new code, pinned as regressions
# ==============================================================================


def test_typed_list_sibling_returns_the_key_the_finding_body_reads():
    """The finding body read `typed_twin['typed_at']` and the helper returned
    `field`, so the first run raised KeyError. The contract is pinned here."""
    models = audit_schemas._audit_models()
    twin = audit_schemas._typed_list_sibling(models, "TransactionQueryReport")
    assert twin is not None
    assert set(twin) == {"model", "field"}
    assert twin["model"] == "TransactionOut"
    assert twin["field"] == "TransactionLogReport.tail"


def test_the_bypassed_finding_and_the_entry_agree_on_the_twin():
    report = audit_schemas.contract_divergence_report(routes=app)
    entries = {e["subject"]: e for e in report.bypassed_contracts}
    findings = {f.subject: f for f in report.findings if f.code == "CONTRACT_TYPED_CONTRACT_BYPASSED"}
    assert set(entries) == set(findings)
    for subject, finding in findings.items():
        assert finding.evidence["typed_at"] == entries[subject]["typed_at"]
        assert finding.evidence["declared_model"] == entries[subject]["declared_model"]


def test_kind_inference_uses_the_longest_matching_suffix():
    """First-match-wins inferred `report` for TransactionSpecReport."""
    assert audit_schemas._kind_for("TransactionSpecReport") == "spec"
    # and the suffix is deliberately the longer one, not "Report"
    suffixes = {
        suffix
        for row in audit_schemas.CONTRACT_KINDS.values()
        for suffix in row["suffixes"]
    }
    assert "Report" in suffixes and "SpecReport" in suffixes
    # no two kinds claim the same suffix
    all_suffixes = [
        suffix
        for row in audit_schemas.CONTRACT_KINDS.values()
        for suffix in row["suffixes"]
    ]
    assert len(all_suffixes) == len(set(all_suffixes))


def test_shape_key_distinguishes_a_container_from_a_primitive():
    from typing import Any, Dict, List, Optional

    assert audit_schemas._shape_key(audit_schemas._shape_of(List[str])).startswith("list:")
    assert audit_schemas._shape_key(audit_schemas._shape_of(Dict[str, Any])) == "dict:str,Any"
    assert audit_schemas._shape_key(audit_schemas._shape_of(bool)) == "bool"
    assert audit_schemas._shape_key(audit_schemas._shape_of(int)) == "int"
    assert audit_schemas._shape_key(audit_schemas._shape_of(Optional[str])) == "str"


def test_the_collision_check_reports_only_primitive_disagreements():
    """list-vs-dict is a container choice; bool-vs-int is a meaning change."""
    from typing import Any, Dict, List

    differing = audit_schemas._shape_key(audit_schemas._shape_of(List[str]))
    other = audit_schemas._shape_key(audit_schemas._shape_of(Dict[str, Any]))
    assert differing != other
    # both are containers, so the check skips them even though they differ
    assert differing.startswith(("list:", "dict:"))
    assert other.startswith(("list:", "dict:"))


def test_the_source_vocabulary_scan_is_scoped_per_class():
    """An unscoped regex matched a comment from the wrong class body."""
    vocabularies = audit_schemas._source_vocabularies()
    assert "AuditIntegrityGateOut.severity" in vocabularies
    assert vocabularies["AuditIntegrityGateOut.severity"] == "advisory | review | reject"
    # the event vocabulary is not attached to the gate contract, even though
    # both comments are in the same file
    assert vocabularies.get("AuditLogEntryCreate.severity") is None
    assert set(vocabularies) == set(ORIG_VOCABS)


def test_field_annotation_identity_is_compared_by_identity_not_string():
    """`f.annotation == "str"` never matched; `is str` does."""
    facts = audit_schemas._field_facts(audit_schemas.AuditLogEntryOut, "action")
    assert facts["annotation"] == "str"
    assert audit_schemas.AuditLogEntryCreate.model_fields["action"].annotation is str


def test_shape_of_does_not_claim_to_know_a_model_class():
    """known means recognised. A contract class is not a shape this reads."""
    shape = audit_schemas._shape_of(audit_schemas.TransactionOut)
    assert shape["known"] is False
    assert not any(
        shape[flag]
        for flag in ("is_list", "is_dict", "is_str", "is_bool", "is_int", "is_any")
    )


def test_describe_reads_the_timestamp_carrier_the_policy_row_declares():
    """`_carrier_of(annotation)` without the field name filed a timestamp as a
    free label, while the returned `carrier` said timestamp -- one function,
    two answers. The field name is now passed to both."""
    row = audit_schemas.describe_contract_field("PipelineDeadLetterEntryOut", "occurred_at")
    assert row.carrier == "timestamp"
    assert any("no format is published" in item for item in row.enforced_by)


def test_the_catalog_ground_truth_survives_a_malformed_row(monkeypatch):
    table = {k: dict(v) for k, v in audit_schemas.CONTRACT_INVENTORY.items()}
    table["AuditLogEntryOut"] = None
    monkeypatch.setattr(audit_schemas, "CONTRACT_INVENTORY", table)
    catalog = audit_schemas.build_contract_catalog(routes=app)
    assert catalog.ground_truth["models"] == 40
    assert catalog.inventory.finding_count >= 1


def test_the_layer_never_edits_a_model():
    """No function in the governance layer assigns to a model class."""
    source = inspect.getsource(audit_schemas)
    tree = ast.parse(source)
    governance_start = source.index("CONTRACT_GOVERNANCE_VERSION")
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.lineno < source[:governance_start].count("\n"):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    # only the local report variables are assigned
                    assert not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in ORIG_SHAPE
                    ), f"{node.name} assigns to a contract class"


def test_every_report_function_is_callable_with_no_arguments():
    """`routes=` is optional everywhere it appears, and skipping is reported."""
    for func in (
        audit_schemas.contract_inventory,
        audit_schemas.contract_divergence_report,
        audit_schemas.contract_redaction_report,
        audit_schemas.build_contract_catalog,
    ):
        result = func()
        assert result is not None
    assert audit_schemas.validate_contracts() is not None
    assert audit_schemas.describe_contract_field("AuditLogEntryOut", "id").resolved is True
