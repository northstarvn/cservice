from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from datetime import datetime


class ComponentMetrics(BaseModel):
    """Efficiency metrics for one scanned component (static/conceptual view)."""

    component: str
    kind: str
    description: str
    paths_used: List[str] = Field(default_factory=list)
    present: bool = False
    file_count: int = 0
    line_count: int = 0
    avg_file_lines: float = 0.0
    max_file_lines: int = 0
    largest_file: str = ""
    marker_count: int = 0
    marker_density_per_1000: float = 0.0
    test_files: int = 0
    test_ratio: float = 0.0
    efficiency_score: float = 0.0
    classification: str = "missing"  # efficient | needs_attention | at_risk | missing
    findings: List[str] = Field(default_factory=list)


class SystemDataPoint(BaseModel):
    metric: str
    value: int


class SystemDataSection(BaseModel):
    available: bool = False
    source: str = ""
    error: Optional[str] = None
    points: List[SystemDataPoint] = Field(default_factory=list)


class LogScanResult(BaseModel):
    available: bool = False
    paths_scanned: List[str] = Field(default_factory=list)
    file_count: int = 0
    error_lines: int = 0
    warning_lines: int = 0
    error_density_per_1000: float = 0.0
    samples: List[str] = Field(default_factory=list)


class EnhancementProposal(BaseModel):
    id: str
    code: str
    component: str
    title: str
    priority: str  # high | medium | low
    impact: str  # high | medium | low
    effort: str  # high | medium | low
    rationale: List[str] = Field(default_factory=list)
    recommended_action: str
    owner_hint: str
    signals: List[str] = Field(default_factory=list)


class SystemEfficiencyReport(BaseModel):
    generated_at: datetime
    scope: str
    method: str
    rule_version: str
    data_sources: Dict[str, str] = Field(default_factory=dict)
    components: List[ComponentMetrics] = Field(default_factory=list)
    system_data: SystemDataSection = Field(default_factory=SystemDataSection)
    logs: LogScanResult = Field(default_factory=LogScanResult)
    summary: Dict[str, Any] = Field(default_factory=dict)
    enhancements: List[EnhancementProposal] = Field(default_factory=list)


class EnhancementListReport(BaseModel):
    generated_at: datetime
    rule_version: str
    total: int
    by_priority: Dict[str, int] = Field(default_factory=dict)
    items: List[EnhancementProposal] = Field(default_factory=list)


# --- Audit trail ---------------------------------------------------------------


class AuditLogEntryCreate(BaseModel):
    action: str = Field(..., min_length=1, max_length=80)
    entity_type: str = Field(default="system", max_length=50)
    entity_id: str = Field(default="", max_length=120)
    summary: str = Field(..., min_length=1)
    detail: Dict[str, Any] = Field(default_factory=dict)
    severity: str = Field(default="info", pattern="^(info|warning|critical)$")
    source: str = Field(default="api", max_length=50)


class AuditLogEntryOut(BaseModel):
    id: int
    actor_user_id: Optional[int] = None
    action: str
    entity_type: str
    entity_id: str
    summary: str
    detail: Dict[str, Any] = Field(default_factory=dict)
    severity: str
    source: str
    created_at: datetime


class AuditLogListReport(BaseModel):
    generated_at: datetime
    total: int
    limit: int
    offset: int
    entries: List[AuditLogEntryOut] = Field(default_factory=list)


class AuditLogSummaryItem(BaseModel):
    key: str
    count: int


class AuditLogSummaryReport(BaseModel):
    generated_at: datetime
    total: int
    by_action: List[AuditLogSummaryItem] = Field(default_factory=list)
    by_severity: List[AuditLogSummaryItem] = Field(default_factory=list)


# --- Governed writes & forensic reads -----------------------------------------
#
# These cover the expanded ``services/audit_log.py`` surface: validated/sealed
# writes, tamper verification, actor rollups, entity timelines, anomaly
# findings, retention planning, and export.


class AuditableLogEntryCreate(BaseModel):
    """Governed write: aliases resolve, sensitive detail must justify itself."""

    action: str = Field(..., min_length=1, max_length=80)
    entity_type: str = Field(default="system", max_length=50)
    entity_id: str = Field(default="", max_length=120)
    summary: str = Field(..., min_length=1)
    detail: Dict[str, Any] = Field(default_factory=dict)
    severity: Optional[str] = Field(
        default=None, pattern="^(info|warning|critical)$"
    )
    source: str = Field(default="api", max_length=50)
    strict: bool = Field(
        default=False, description="Reject actions outside the audit catalog"
    )
    require_justification: bool = Field(
        default=True, description="Enforce the action's justification requirement"
    )
    seal: bool = Field(default=True, description="Stamp the hash-chain seal")


class AuditLogIntegrityReport(BaseModel):
    generated_at: datetime
    valid: bool
    entries: int
    broken_at: Optional[int] = None
    algo: str
    policy: str
    sealed_fields: List[str] = Field(default_factory=list)


class AuditActorActivityOut(BaseModel):
    actor_user_id: Optional[int] = None
    count: int
    distinct_entities: int
    by_action: Dict[str, int] = Field(default_factory=dict)
    by_severity: Dict[str, int] = Field(default_factory=dict)
    peak_severity: str = "info"
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None


class AuditActorActivityReport(BaseModel):
    generated_at: datetime
    actor_count: int
    actors: List[AuditActorActivityOut] = Field(default_factory=list)


class AuditTimelineEventOut(BaseModel):
    id: Optional[int] = None
    action: str
    actor_user_id: Optional[int] = None
    severity: str
    summary: str = ""
    source: str = ""
    occurred_at: Optional[datetime] = None
    changed: Optional[bool] = None


class AuditTimelineReport(BaseModel):
    generated_at: datetime
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    count: int
    events: List[AuditTimelineEventOut] = Field(default_factory=list)


class AuditAnomalyFindingOut(BaseModel):
    rule: str
    severity: str
    subject: str
    message: str
    count: int = 0
    entry_ids: List[Optional[int]] = Field(default_factory=list)
    observed_at: Optional[datetime] = None


class AuditAnomalyReport(BaseModel):
    generated_at: datetime
    scanned: int
    finding_count: int
    rules: List[str] = Field(default_factory=list)
    thresholds: Dict[str, Any] = Field(default_factory=dict)
    findings: List[AuditAnomalyFindingOut] = Field(default_factory=list)


class AuditRetentionRecordOut(BaseModel):
    id: Optional[int] = None
    action: str
    severity: str
    age_days: float
    retention_days: int
    expires_on: datetime


class AuditRetentionReport(BaseModel):
    generated_at: datetime
    now: datetime
    expired_count: int
    keep_count: int
    expired: List[AuditRetentionRecordOut] = Field(default_factory=list)
    keep: List[AuditRetentionRecordOut] = Field(default_factory=list)
    policies: Dict[str, int] = Field(default_factory=dict)
    retention_by_severity: Dict[str, int] = Field(default_factory=dict)
    minimum_days: int


class AuditLogExportReport(BaseModel):
    format: str
    entry_count: int
    page_size: int
    max_pages: int
    truncated: bool
    columns: Optional[List[str]] = None
    content: str


# --- High-velocity audit pipeline ---------------------------------------------


class PipelineEventCreate(BaseModel):
    kind: str = Field(..., min_length=1, max_length=80, description="Signal family, e.g. auth.login.risk")
    severity: str = Field(default="info", pattern="^(info|warning|critical)$")
    entity_type: str = Field(default="system", max_length=50)
    entity_id: str = Field(default="", max_length=120)
    actor_user_id: Optional[int] = None
    tenant_id: Optional[str] = Field(default=None, max_length=64)
    payload: Dict[str, Any] = Field(default_factory=dict)


class PipelineEventAccepted(BaseModel):
    accepted: bool
    event_id: str
    reason: Optional[str] = None


class PipelineStatsReport(BaseModel):
    generated_at: datetime
    name: str
    running: bool
    workers: int
    batch_size: int
    max_queue: int
    flush_seconds: float
    pending: int
    submitted: int
    processed: int
    batches: int
    rejected: int
    dead_lettered: int


class TransactionOut(BaseModel):
    version: int
    tx_id: str
    cursor: int
    action: str
    entity_type: str
    entity_id: str
    actor_user_id: Optional[int] = None
    tenant_id: Optional[str] = None
    occurred_at: datetime
    payload: Dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    signature_present: bool


class TransactionLogReport(BaseModel):
    generated_at: datetime
    total: int
    tail: List[TransactionOut] = Field(default_factory=list)
    chain: Dict[str, Any] = Field(default_factory=dict)


class TransactionSpecReport(BaseModel):
    spec: Dict[str, Any]


# --- Read-side projections & integrity gates -----------------------------------
#
# `/audit/logs` is unchanged and still returns whole entries. These contracts
# back the additive read surface: a profile projection (`/audit/logs/view`) and
# the config-driven gate table over the trail (`/audit/integrity/gates`).


class AuditProjectedEntryOut(BaseModel):
    """One entry as a view profile renders it.

    Which fields appear is decided by the profile, so the model allows for
    exactly that set and no more. ``detail`` is ``None`` in ``omit`` mode, a
    type-only shape in ``shape`` mode, and a redacted payload in ``full`` mode.
    """

    profile: str
    detail_mode: str
    detail_keys: List[str] = Field(default_factory=list)
    redacted: bool = False
    truncated: bool = False
    id: Optional[int] = None
    action: str = ""
    severity: str = "info"
    entity_type: str = ""
    entity_id: str = ""
    actor_user_id: Optional[int] = None
    source: Optional[str] = None
    summary: Optional[str] = None
    detail: Optional[Any] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    sealed: Optional[bool] = None


class AuditViewReport(BaseModel):
    """A batch of entries projected through one profile."""

    generated_at: datetime
    profile: str
    detail_mode: str
    fields: List[str] = Field(default_factory=list)
    max_summary_chars: Optional[int] = None
    include_seal: bool = False
    count: int
    redacted_count: int = 0
    truncated_count: int = 0
    sealed_count: int = 0
    entries: List[AuditProjectedEntryOut] = Field(default_factory=list)
    note: str = ""


class AuditIntegrityGateOut(BaseModel):
    """One evaluated row of the integrity gate table."""

    gate_id: str
    metric: str
    op: str
    op_meaning: str = ""
    threshold: Optional[Any] = None
    actual: Optional[Any] = None
    observed: bool = False
    severity: str = "advisory"  # advisory | review | reject
    holds: bool = False
    reason: str = ""  # ok | threshold_not_met | metric_missing
    rationale: str = ""


class AuditGateIntegrityReport(BaseModel):
    """Is this trail trustworthy right now, and which gate says otherwise."""

    generated_at: datetime
    verdict: str = "clean"  # clean | review | reject
    rejected_by: List[str] = Field(default_factory=list)
    needs_review_by: List[str] = Field(default_factory=list)
    advisory_by: List[str] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    gates: List[AuditIntegrityGateOut] = Field(default_factory=list)
    gates_evaluated: int = 0
    summary: str = ""


# --- Dead-letter inspection & replay -------------------------------------------
#
# `/audit/pipeline/event` and `/audit/pipeline/stats` are unchanged. The ring is
# inspectable and drainable now; the report and the replay result are separate
# contracts because one is a read and one is a mutation.


class PipelineDeadLetterEntryOut(BaseModel):
    event_id: str
    kind: str
    occurred_at: Optional[str] = None
    error: str = ""
    severity: str = "info"
    entity_type: str = "system"
    entity_id: str = ""
    actor_user_id: Optional[int] = None
    tenant_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    route: str = ""
    replayable: bool = True
    attempts: int = 0
    first_failed_at: Optional[str] = None
    last_failed_at: Optional[str] = None


class PipelineDeadLetterReport(BaseModel):
    """Why the dead-letter ring is the size it is, without touching it."""

    dead_lettered: int = 0
    retained: int = 0
    replayable: int = 0
    policy: Dict[str, Any] = Field(default_factory=dict)
    recent: List[PipelineDeadLetterEntryOut] = Field(default_factory=list)
    generated_at: Optional[datetime] = None
    ring_capacity: int = 0
    evicted: int = 0
    replayable_ratio: Optional[float] = None
    blocked_by_policy: int = 0
    max_attempts_seen: int = 0
    by_kind: Dict[str, int] = Field(default_factory=dict)
    by_error: Dict[str, int] = Field(default_factory=dict)
    oldest_occurred_at: Optional[str] = None
    newest_occurred_at: Optional[str] = None
    drainable: bool = False
    note: str = ""


class PipelineReplayRequest(BaseModel):
    """Replay bounds. Omitted means "use the configured policy"."""

    max_attempts: Optional[int] = Field(default=None, ge=1, le=100)
    backoff_seconds: Optional[float] = Field(default=None, ge=0.0, le=3600.0)
    dry_run: bool = Field(
        default=False,
        description="Report what would be re-queued and leave the ring untouched",
    )


class PipelineReplayReport(BaseModel):
    """What a replay did (or, with ``dry_run``, would have done)."""

    generated_at: datetime
    dry_run: bool = False
    attempted: int = 0
    requeued: List[str] = Field(default_factory=list)
    exhausted: List[str] = Field(default_factory=list)
    exhausted_count: int = 0
    max_attempts: int = 0
    backoff_seconds: float = 0.0
    remaining: int = 0
    policy: Dict[str, Any] = Field(default_factory=dict)
    note: str = ""


# --- Transaction query, integrity & export --------------------------------------
#
# `/audit/transactions` (tail + chain) and `/audit/transactions/spec` are
# unchanged. These expose the query/integrity/export surface of the same log.


class TransactionQueryReport(BaseModel):
    """A filtered, sorted, paginated slice of the transaction log."""

    generated_at: datetime
    total: int
    matched: int
    offset: int
    limit: int
    order: str = "desc"
    sort: str = "cursor"
    view: str = "internal"
    filters: List[Dict[str, Any]] = Field(default_factory=list)
    results: List[Dict[str, Any]] = Field(default_factory=list)


class TransactionIntegrityReport(BaseModel):
    """Chain, checkpoint, schema and signing state of the transaction log."""

    generated_at: datetime
    chain: Dict[str, Any] = Field(default_factory=dict)
    checkpoints: List[Dict[str, Any]] = Field(default_factory=list)
    checkpoint_interval: int = 0
    spec_versions: List[int] = Field(default_factory=list)
    sampled: List[Dict[str, Any]] = Field(default_factory=list)
    unsigned_but_required: List[str] = Field(default_factory=list)
    append_only: bool = True
    total: int = 0
    verdict: str = "clean"  # clean | review | reject
    findings: List[str] = Field(default_factory=list)


class TransactionExportReport(BaseModel):
    """A serialised export plus the parameters needed to reproduce it."""

    generated_at: datetime
    format: str
    view: str
    frame_count: int
    filters: List[Dict[str, Any]] = Field(default_factory=list)
    reimportable: bool = False
    signature_survives: bool = False
    bytes: int = 0
    content: str
    note: str = ""


# --- Contract governance -----------------------------------------------------
#
# Everything above this line is a *shipped* pydantic contract: it is what a
# client binds to, and it does not change. Everything below describes those
# contracts, and is deliberately unable to change them.
#
# The posture is **report, never repair**, and for a sharper reason than in the
# i18n module. A pydantic model here is not a rendering of a policy -- it *is*
# the contract. Widening ``severity`` from ``pattern=^(info|warning|critical)$``
# to a plain ``str`` to silence a finding would not make the audit trail more
# honest; it would delete the only place the vocabulary was written down and
# start accepting a fourth severity at the door. So every operation below is
# ``report_only`` and :func:`validate_contracts` *errors* on a row that is not.
#
# The detectors are structural: they read this module's own classes through
# ``model_fields`` and the source through :mod:`ast`. Nothing here imports
# ``app.models`` or ``app.main``. The database constraints and the route table
# are facts this module does not own, so they are recorded in the tables as
# documentation and explicitly *not* verified here -- a schemas module that
# claims to have checked the DDL would be a lie with a passing test.
#
# Ground truth as of this writing (asserted in the tests, so a future edit to
# any model above moves them loudly rather than silently):
#   * 40 models: 4 request bodies, 22 top-level responses, 14 that can only
#     ever appear as a field of another contract. 4 + 22 + 14 = 40, and the
#     taxonomy's ``expect_generated_at`` applies only to the 19 reports and the
#     1 acknowledgement -- a ``spec`` contract is a computed document, not a
#     reading, so TransactionSpecReport is not expected to stamp itself.
#   * 4 write routes: POST /audit/log, /audit/log/auditable, /audit/pipeline/
#     event, /audit/pipeline/replay.
#   * ``POST /audit/log`` is the only audit write whose 2xx response has no
#     declared schema at all; the other three return PipelineEventAccepted,
#     PipelineReplayReport and AuditLogEntryOut.
#   * 18 models declare generated_at; PipelineDeadLetterReport makes it
#     Optional where the other 17 require it, and AuditLogExportReport and
#     PipelineEventAccepted omit it entirely.
#   * the only placeholder in the whole module with a nullable element type is
#     ``AuditAnomalyFindingOut.entry_ids: List[Optional[int]]``.
#   * 8 fields declare a vocabulary in a trailing comment only, while 3 sibling
#     ``severity`` fields in the same module use a real ``pattern=`` -- and
#     ``severity`` itself carries two vocabularies, because
#     ``AuditIntegrityGateOut.severity`` is advisory|review|reject while every
#     other severity in the module is info|warning|critical.
#   * 5 timestamp-shaped fields are ``str`` and all 5 are in the dead-letter
#     family; the other 30 are ``datetime``.


def _audit_models() -> Dict[str, type]:
    """The pydantic models this module defines, name -> class.

    Scans this module's own globals rather than importing a registry, so a
    model added above shows up here with no bookkeeping. Classes *imported* into
    the module are excluded: only what is declared here is this module's
    contract, and a vendored base class is not a contract.

    :data:`GOVERNANCE_MODELS` are excluded too, and the exclusion is declared
    rather than inferred from a banner comment. A governance report is a
    *description* of contracts; counting it as one would inflate the inventory
    with 8 rows that no client binds to and no route serves -- and would make
    the count meaningless in exactly the way an audit surface must not.
    """

    out: Dict[str, type] = {}
    for name, obj in list(globals().items()):
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseModel)
            and obj is not BaseModel
            and obj.__module__ == __name__
            and name not in GOVERNANCE_MODELS
        ):
            out[name] = obj
    return dict(sorted(out.items()))


def _field_facts(model: type, name: str) -> Dict[str, Any]:
    """Everything about one field that the tables can be judged against.

    Reads ``model_fields`` rather than the source, so a field is described the
    way pydantic actually built it. A field this function cannot resolve is
    reported as unknown instead of raising: a governance table that dies on a
    malformed row is worse than one that says the row is malformed.
    """

    facts: Dict[str, Any] = {
        "model": model.__name__,
        "field": name,
        "annotation": "unknown",
        "required": None,
        "default_repr": None,
        "description": None,
        "min_length": None,
        "max_length": None,
        "pattern": None,
        "ge": None,
        "le": None,
        "comment_vocabulary": None,
        "resolved": False,
    }
    try:
        fields = getattr(model, "model_fields", {}) or {}
        field = fields.get(name)
        if field is None:
            return facts
        annotation = field.annotation
        facts["annotation"] = (
            annotation if isinstance(annotation, str) else getattr(annotation, "__name__", None) or str(annotation)
        )
        facts["annotation_full"] = str(annotation)
        facts["required"] = field.is_required()
        default = field.default
        facts["default_repr"] = None if repr(default).startswith("PydanticUndefined") else repr(default)
        facts["description"] = field.description
        for constraint in list(getattr(field, "metadata", []) or []):
            for attr, key in (
                ("min_length", "min_length"),
                ("max_length", "max_length"),
                ("pattern", "pattern"),
                ("ge", "ge"),
                ("le", "le"),
            ):
                value = getattr(constraint, attr, None)
                if value is not None and facts[key] is None:
                    facts[key] = value
    except Exception as exc:  # noqa: BLE001 -- a report must survive its own input
        facts["error"] = f"{type(exc).__name__}: {exc}"
        return facts
    facts["resolved"] = True
    return facts


def _source_vocabularies() -> Dict[str, Dict[str, str]]:
    """Trailing-comment vocabularies, keyed ``"Model.field"``.

    Read from the source with :mod:`ast` so the lookup is scoped to the class
    body that declares the field. A regex over the whole file cannot tell
    ``AuditLogEntryOut.severity`` from ``AuditIntegrityGateOut.severity`` -- and
    those two carry *different* vocabularies under the *same* name, which is
    exactly the collision this table exists to report.
    """

    out: Dict[str, str] = {}
    try:
        import ast

        with open(__file__, "r", encoding="utf-8") as handle:
            source = handle.read()
        lines = source.split("\n")
        for node in ast.parse(source).body:
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                    continue
                line = lines[stmt.lineno - 1] if stmt.lineno - 1 < len(lines) else ""
                if "#" not in line:
                    continue
                comment = line.split("#", 1)[1].strip()
                if "|" not in comment:
                    continue
                out[f"{node.name}.{stmt.target.id}"] = comment
    except Exception:  # noqa: BLE001 -- source drift must not break the report
        return {}
    return out


#: What role a model plays, keyed by the suffix it is named with. ``direction``
#: is the only field with teeth: a ``create`` contract is input, so an uncapped
#: string in one is a defect, while the same string in an ``out`` contract is
#: merely unbounded output. The other columns are the invariants the structural
#: reports check.
CONTRACT_KINDS: Dict[str, Dict[str, Any]] = {
    "create": {
        "suffixes": ("Create", "Request"),
        "direction": "in",
        "may_be_top_level": False,
        "must_be_a_request_body": True,
        "expect_generated_at": False,
        "summary": "a request body; a client binds to it to make a call",
    },
    "out": {
        "suffixes": ("Out",),
        "direction": "out",
        "may_be_top_level": True,
        "must_be_a_request_body": False,
        "expect_generated_at": False,
        "summary": "one record of a collection, or a whole entry",
    },
    "report": {
        "suffixes": ("Report",),
        "direction": "out",
        "may_be_top_level": True,
        "must_be_a_request_body": False,
        "expect_generated_at": True,
        "summary": "an envelope wrapping a list or a block of computed values",
    },
    "item": {
        "suffixes": ("Item",),
        "direction": "out",
        "may_be_top_level": True,
        "must_be_a_request_body": False,
        "expect_generated_at": False,
        "summary": "a key/count pair inside a summary report",
    },
    "accepted": {
        "suffixes": ("Accepted",),
        "direction": "out",
        "may_be_top_level": True,
        "must_be_a_request_body": False,
        "expect_generated_at": True,
        "summary": "the acknowledgement of a mutation; carries an id, not a body",
    },
    "spec": {
        "suffixes": ("SpecReport",),
        "direction": "out",
        "may_be_top_level": True,
        "must_be_a_request_body": False,
        "expect_generated_at": False,
        "summary": (
            "a computed spec handed back verbatim; nothing validates it. The "
            "suffix is SpecReport, not Report, because TransactionSpecReport "
            "ends in Report and would otherwise infer the wrong kind"
        ),
    },
}

#: Who is allowed to read a contract, and what they are owed. ``min_privilege``
#: is ordered from widest to narrowest and is the only place the read-audience
#: asymmetry is written down: ``/audit/logs`` and ``/audit/logs/view`` serve the
#: same table to different audiences, and the projection is the narrower one.
CONTRACT_AUDIENCES: Dict[str, Dict[str, Any]] = {
    "machine_client": {
        "rank": 0,
        "description": "a generated client binding to the OpenAPI document",
        "expects": "a declared schema on every 2xx it reads",
        "redaction": "none needed -- it should never have been sent a secret",
    },
    "operator": {
        "rank": 1,
        "description": "a dashboard or on-call console",
        "expects": "a generated_at so the view can say how old it is",
        "redaction": "shape-only detail is enough",
    },
    "pipeline_operator": {
        "rank": 1,
        "description": "whoever drains the dead-letter ring",
        "expects": "replayable, attempts and a bounded request",
        "redaction": "the payload is the evidence; keep it",
    },
    "investigator": {
        "rank": 2,
        "description": "an incident responder asking what a specific entry said",
        "expects": "full detail, and the seal",
        "redaction": "credential-shaped keys are already scrubbed at write time",
    },
    "auditor": {
        "rank": 2,
        "description": "a compliance reader of the immutable trail",
        "expects": "chain, checkpoints and signature state",
        "redaction": "hashes yes, payload contents no",
    },
}

#: Policy per *field name*, because policy attaches to names that recur: there
#: are 5 ``generated_at`` fields, 13 ``severity`` fields and 3 free-form detail
#: carriers, and they do not agree with each other. ``enforcement`` is the
#: honest answer to "where is this actually checked?", and the three values are
#: deliberately distinct:
#:
#: ``pattern``        the schema rejects a value outside the vocabulary.
#: ``column_check``   a database CHECK constraint rejects it. *Not verified
#:                    here* -- see the module header -- but recorded, because
#:                    the write path and the schema would otherwise look like
#:                    the only two places that care.
#: ``comment_only``   a trailing comment lists the values and nothing enforces
#:                    them. This is the finding: the same module uses a real
#:                    ``pattern=`` on three ``severity`` fields while eight
#:                    fields name their vocabulary in a comment.
#: ``declared``       the value is a free label on purpose.
CONTRACT_FIELD_POLICIES: Dict[str, Dict[str, Any]] = {
    "severity": {
        "carrier": "scalar",
        "vocabulary": ("info", "warning", "critical"),
        "declared_in": "pattern",
        "note": (
            "the audit event vocabulary; the ONLY severity column the database "
            "protects (ck_audit_log_entries_severity_valid) -- asserted here, "
            "not verified by this module"
        ),
    },
    "action": {
        "carrier": "scalar",
        "vocabulary": None,
        "declared_in": "declared",
        "note": "80 chars, aliased and catalog-validated on the governed write path",
    },
    "summary": {
        "carrier": "scalar",
        "vocabulary": None,
        "declared_in": "declared",
        "note": (
            "the one uncapped string on the write path: min_length=1, no "
            "max_length, backed by a Text column. The name is also used for a "
            "Dict[str, Any] in SystemEfficiencyReport, so the policy row "
            "describes the string and the collision is reported separately"
        ),
    },
    "detail": {
        "carrier": "blob",
        "vocabulary": None,
        "declared_in": "declared",
        "note": (
            "free-form Dict[str, Any]; redacted on the governed write path and "
            "stored verbatim on the ungated one"
        ),
    },
    "payload": {
        "carrier": "blob",
        "vocabulary": None,
        "declared_in": "declared",
        "note": "the pipeline's free-form carrier; no size, depth or item bound is declared",
    },
    "content": {
        "carrier": "embedded",
        "vocabulary": None,
        "declared_in": "declared",
        "note": (
            "a whole serialised export inside a JSON response, typed as an "
            "unbounded str rather than a structure. 'embedded' is its own "
            "carrier class: it is not a scalar, and pretending it was is how a "
            "whole export slips past a size check that only looks at str fields"
        ),
    },
    "generated_at": {
        "carrier": "timestamp",
        "vocabulary": None,
        "declared_in": "declared",
        "note": (
            "the envelope's age stamp; 18 models declare it -- required on 17, "
            "Optional on PipelineDeadLetterReport -- and 2 envelopes omit it "
            "entirely (AuditLogExportReport, PipelineEventAccepted)"
        ),
    },
    "occurred_at": {
        "carrier": "timestamp",
        "vocabulary": None,
        "declared_in": "declared",
        "note": (
            "declared datetime everywhere except the dead-letter family, which "
            "stores ISO strings and re-parses them with _parse_moment"
        ),
    },
    "first_failed_at": {
        "carrier": "timestamp",
        "vocabulary": None,
        "declared_in": "declared",
        "note": "an ISO string by construction (datetime.now().isoformat()); no format is published",
    },
    "actor_user_id": {
        "carrier": "scalar",
        "vocabulary": None,
        "declared_in": "declared",
        "note": "nullable: the column is ON DELETE SET NULL, so an entry outlives its actor",
    },
    "tenant_id": {
        "carrier": "scalar",
        "vocabulary": None,
        "declared_in": "declared",
        "note": "64 chars on the write path, unbounded on the way out",
    },
    "prev_hash": {
        "carrier": "scalar",
        "vocabulary": None,
        "declared_in": "declared",
        "note": "the chain link; exposed deliberately so a client can verify it",
    },
    "replayable": {
        "carrier": "scalar",
        "vocabulary": None,
        "declared_in": "declared",
        "note": (
            "a bool flag on the entry and an int count on the report -- the same "
            "name meaning two different things, in opposite types"
        ),
    },
    "entry_ids": {
        "carrier": "list",
        "vocabulary": None,
        "declared_in": "declared",
        "note": "the only list in the module typed List[Optional[int]], so [None] validates",
    },
}

#: Names that are self-describing enough that a policy row would say nothing.
#: Listed rather than left to a frequency threshold, because a threshold is
#: arbitrary and would quietly start reporting ``id`` the day somebody added one
#: more model. What is *not* exempt is a name whose shape is contested, and the
#: shape-collision check runs over these names too.
CONTRACT_TRIVIAL_FIELDS: Dict[str, str] = {
    "id": "a primary key, unless a contract says otherwise -- and the shape check says otherwise here",
    "count": "a tally",
    "total": "a tally",
    "note": "free text written for a human reader",
    "findings": "a list of results",
    "kind": "a family label",
    "entries": "a list of records",
    "source": "where the record came from",
    "entity_id": "the id of the thing acted on",
    "entity_type": "the kind of thing acted on",
    "action": "has a policy row",
    "policy": "the configuration in force, in whatever shape the contract chose",
    "rules": "a list of rule names",
    "thresholds": "the configuration a report was judged against",
    "metrics": "computed values a gate was evaluated on",
    "chain": "hash-chain state",
    "filters": "repeated query parameters",
    "spec": "a computed document",
    "data_sources": "where the scan read from",
    "summary": "has a policy row",
    "detail": "has a policy row",
    "payload": "has a policy row",
    "content": "has a policy row",
}

#: Field names that must never be declared on a response contract in this
#: module, and what to publish instead. The detector is a *name* check, not a
#: value check: it cannot see inside ``detail``, which is exactly the point.
#: A contract that declared ``password`` as a field would be publishing the
#: shape of a secret; one that declares ``detail`` is publishing an opaque
#: carrier the writer is responsible for. The check is clean today and is
#: reported as clean rather than omitted, so that a future model that reaches
#: for a convenient name trips it.
CONTRACT_FORBIDDEN_FIELDS: Dict[str, Dict[str, str]] = {
    "detail_json": {
        "reason": "the raw column name; it would hand a client a stringified blob instead of a structure",
        "publish_instead": "detail",
    },
    "password": {
        "reason": "never declared as a field; redact_detail scrubs this key inside a carrier instead",
        "publish_instead": "detail, with the key redacted at write time",
    },
    "token": {
        "reason": "same as password: a declared field is a published shape, a redacted carrier key is not",
        "publish_instead": "detail, with the key redacted at write time",
    },
    "private_key": {
        "reason": "same as password",
        "publish_instead": "detail, with the key redacted at write time",
    },
    "hashed_password": {
        "reason": "a hash is still a credential; it belongs in the vault table, not in a trail contract",
        "publish_instead": "nothing -- refer to the identity record",
    },
    "updated_at_raw": {
        "reason": "an invented raw variant; updated_at is a real column on audit_log_entries",
        "publish_instead": "updated_at",
    },
}

#: The operations this governance layer can perform, and the guarantee each
#: one carries. Every row is ``report_only``: none of them can alter a model
#: above, and :func:`validate_contracts` errors on a row that is not, so the
#: "never repair" posture is a checked property of the table rather than a
#: promise in a docstring.
CONTRACT_OPS: Dict[str, Dict[str, Any]] = {
    "inventory": {
        "op": "_audit_models",
        "report_only": True,
        "reads": "model_fields of every pydantic class declared in this module",
        "cannot": "see a model defined in another module",
    },
    "field_facts": {
        "op": "_field_facts",
        "report_only": True,
        "reads": "pydantic FieldInfo: required, default, description, constraints",
        "cannot": "see a comment; that is what _source_vocabularies is for",
    },
    "source_vocabulary": {
        "op": "_source_vocabularies",
        "report_only": True,
        "reads": "the module's own source, via ast, scoped per class body",
        "cannot": "survive a syntax error -- it returns {} and the reports say so",
    },
    "field_description": {
        "op": "describe_contract_field",
        "report_only": True,
        "reads": "the field-policy table joined to the facts above",
        "cannot": "judge a value; only a declaration",
    },
    "divergence": {
        "op": "contract_divergence_report",
        "report_only": True,
        "reads": "the module's own classes, compared against each other",
        "cannot": "compare against a contract it cannot see -- that needs routes=",
    },
    "redaction": {
        "op": "contract_redaction_report",
        "report_only": True,
        "reads": "the carrier fields, joined to the write paths declared in the inventory",
        "cannot": "read inside a carrier; redaction is the writer's job",
    },
    "validation": {
        "op": "validate_contracts",
        "report_only": True,
        "reads": "every table above, and only the tables above",
        "cannot": "raise on a malformed row; it reports the row instead",
    },
    "catalog": {
        "op": "build_contract_catalog",
        "report_only": True,
        "reads": "the reports and the taxonomy",
        "cannot": "be the only place a finding appears -- each report stands alone",
    },
}

#: One row per shipped model. This is the pinned surface: the inventory reports
#: compare the module against this table, so adding a model without adding a
#: row is a finding rather than an omission. ``route`` names the *effective*
#: path including the router prefix, and ``top_level`` is False for the 14
#: models that can only ever appear as a field of another contract -- a client
#: cannot ask for those directly, which is a fact worth recording rather than
#: leaving to be inferred from a missing route.
CONTRACT_INVENTORY: Dict[str, Dict[str, Any]] = {
    # --- efficiency audit (static scan) -------------------------------------
    "ComponentMetrics": {"kind": "out", "audience": "operator", "route": "", "top_level": False, "fields": 17, "required": 3, "note": "one scanned component; only reachable inside SystemEfficiencyReport"},
    "SystemDataPoint": {"kind": "out", "audience": "operator", "route": "", "top_level": False, "fields": 2, "required": 2, "note": "a single metric value; the smallest contract in the module"},
    "SystemDataSection": {"kind": "out", "audience": "operator", "route": "", "top_level": False, "fields": 4, "required": 0, "note": "carries its own error string, so unavailable is a value and not a 500"},
    "LogScanResult": {"kind": "out", "audience": "operator", "route": "", "top_level": False, "fields": 7, "required": 0, "note": "every field defaulted, including available=False"},
    "EnhancementProposal": {"kind": "out", "audience": "operator", "route": "", "top_level": False, "fields": 11, "required": 9, "note": "priority/impact/effort are three comment-only vocabularies"},
    "SystemEfficiencyReport": {"kind": "report", "audience": "operator", "route": "GET /audit/efficiency", "top_level": True, "fields": 10, "required": 4, "note": "the one report that is genuinely 'generated' rather than 'queried'"},
    "EnhancementListReport": {"kind": "report", "audience": "operator", "route": "GET /audit/enhancements", "top_level": True, "fields": 5, "required": 3, "note": "shares EnhancementProposal with the efficiency report"},
    # --- trail writes ---------------------------------------------------------
    "AuditLogEntryCreate": {"kind": "create", "audience": "machine_client", "route": "POST /audit/log", "top_level": False, "fields": 7, "required": 2, "note": "the ungated write: detail is stored verbatim and the 201 has no declared schema"},
    "AuditableLogEntryCreate": {"kind": "create", "audience": "investigator", "route": "POST /audit/log/auditable", "top_level": False, "fields": 10, "required": 2, "note": "the same trail through the governed path: redacted, sealed, and returns a typed entry"},
    # --- trail reads ----------------------------------------------------------
    "AuditLogEntryOut": {"kind": "out", "audience": "operator", "route": "POST /audit/log/auditable", "top_level": True, "fields": 10, "required": 8, "note": "8 of 10 fields required: the only near-fully-required row contract in the module"},
    "AuditLogListReport": {"kind": "report", "audience": "operator", "route": "GET /audit/logs", "top_level": True, "fields": 5, "required": 4, "note": "returns whole entries; redaction is not on this path"},
    "AuditLogSummaryItem": {"kind": "item", "audience": "operator", "route": "", "top_level": False, "fields": 2, "required": 2, "note": "a key/count pair, used for both by_action and by_severity"},
    "AuditLogSummaryReport": {"kind": "report", "audience": "auditor", "route": "GET /audit/logs/summary", "top_level": True, "fields": 4, "required": 2, "note": "counts only; no content crosses this contract"},
    "AuditLogIntegrityReport": {"kind": "report", "audience": "auditor", "route": "GET /audit/logs/integrity", "top_level": True, "fields": 7, "required": 5, "note": "broken_at is Optional[int] -- an entry id, not a moment, despite the _at suffix"},
    "AuditActorActivityOut": {"kind": "out", "audience": "auditor", "route": "", "top_level": False, "fields": 8, "required": 2, "note": "actor_user_id is the only key, so an actor whose row was SET NULL to NULL collapses"},
    "AuditActorActivityReport": {"kind": "report", "audience": "auditor", "route": "GET /audit/logs/actors", "top_level": True, "fields": 3, "required": 2, "note": "the narrowest envelope: three fields"},
    "AuditTimelineEventOut": {"kind": "out", "audience": "investigator", "route": "", "top_level": False, "fields": 8, "required": 2, "note": "changed is Optional[bool]: None means 'not a diff event', which is a third state"},
    "AuditTimelineReport": {"kind": "report", "audience": "investigator", "route": "GET /audit/logs/timeline", "top_level": True, "fields": 5, "required": 2, "note": "entity_type/entity_id echo the filter back so the client can confirm it"},
    "AuditAnomalyFindingOut": {"kind": "out", "audience": "operator", "route": "", "top_level": False, "fields": 7, "required": 4, "note": "entry_ids is List[Optional[int]] -- the only nullable-element list in the module"},
    "AuditAnomalyReport": {"kind": "report", "audience": "operator", "route": "GET /audit/logs/anomalies", "top_level": True, "fields": 6, "required": 3, "note": "thresholds is Dict[str, Any]: the report shows its own configuration inline"},
    "AuditRetentionRecordOut": {"kind": "out", "audience": "operator", "route": "", "top_level": False, "fields": 6, "required": 5, "note": "5 of 6 required, because every one of these is computed rather than optional"},
    "AuditRetentionReport": {"kind": "report", "audience": "operator", "route": "GET /audit/logs/retention", "top_level": True, "fields": 9, "required": 5, "note": "expired and keep are the same type used for two opposite buckets"},
    "AuditLogExportReport": {"kind": "report", "audience": "auditor", "route": "GET /audit/logs/export", "top_level": True, "fields": 7, "required": 6, "note": "one of 3 reports with no generated_at, and it embeds content: str"},
    "AuditProjectedEntryOut": {"kind": "out", "audience": "investigator", "route": "", "top_level": False, "fields": 17, "required": 2, "note": "a union of every profile: detail is Optional[Any] because its mode decides its type"},
    "AuditViewReport": {"kind": "report", "audience": "investigator", "route": "GET /audit/logs/view", "top_level": True, "fields": 12, "required": 4, "note": "the profile projection; the narrower read of the same table as /audit/logs"},
    "AuditIntegrityGateOut": {"kind": "out", "audience": "operator", "route": "", "top_level": False, "fields": 11, "required": 3, "note": "severity here is advisory|review|reject, NOT the event vocabulary -- see CONTRACT_FIELD_POLICIES"},
    "AuditGateIntegrityReport": {"kind": "report", "audience": "operator", "route": "GET /audit/integrity/gates", "top_level": True, "fields": 9, "required": 1, "note": "one required field of nine; verdict is a comment-only vocabulary"},
    # --- high-velocity pipeline ----------------------------------------------
    "PipelineEventCreate": {"kind": "create", "audience": "machine_client", "route": "POST /audit/pipeline/event", "top_level": False, "fields": 7, "required": 1, "note": "no occurred_at: the server stamps it, so a client cannot backdate an event"},
    "PipelineEventAccepted": {"kind": "accepted", "audience": "machine_client", "route": "POST /audit/pipeline/event", "top_level": True, "fields": 3, "required": 2, "note": "a 202 ack; the only contract in the module that is both a kind and a status code"},
    "PipelineStatsReport": {"kind": "report", "audience": "pipeline_operator", "route": "GET /audit/pipeline/stats", "top_level": True, "fields": 13, "required": 13, "note": "all 13 required: a counter never has a default, so a missing one is a schema error"},
    "PipelineDeadLetterEntryOut": {"kind": "out", "audience": "pipeline_operator", "route": "", "top_level": False, "fields": 15, "required": 2, "note": "all three timestamps are Optional[str]: the ring stores ISO strings"},
    "PipelineDeadLetterReport": {"kind": "report", "audience": "pipeline_operator", "route": "GET /audit/pipeline/dead-letters", "top_level": True, "fields": 17, "required": 0, "note": "all 17 defaulted, and generated_at is Optional[datetime] where the other 17 reports require it"},
    "PipelineReplayRequest": {"kind": "create", "audience": "pipeline_operator", "route": "POST /audit/pipeline/replay", "top_level": False, "fields": 3, "required": 0, "note": "every field optional means 'use the configured policy'; the only fully-bounded request in the module"},
    "PipelineReplayReport": {"kind": "report", "audience": "pipeline_operator", "route": "POST /audit/pipeline/replay", "top_level": True, "fields": 11, "required": 1, "note": "the only report that is the response to a mutation"},
    # --- transaction log ------------------------------------------------------
    "TransactionOut": {"kind": "out", "audience": "auditor", "route": "", "top_level": False, "fields": 12, "required": 9, "note": "exposes prev_hash and signature_present but not the signature"},
    "TransactionLogReport": {"kind": "report", "audience": "auditor", "route": "GET /audit/transactions", "top_level": True, "fields": 4, "required": 2, "note": "tail is typed List[TransactionOut]; the query route returns the same frames untyped"},
    "TransactionSpecReport": {"kind": "spec", "audience": "auditor", "route": "GET /audit/transactions/spec", "top_level": True, "fields": 1, "required": 1, "note": "a single Dict[str, Any]; the spec is computed and nothing in this module validates it"},
    "TransactionQueryReport": {"kind": "report", "audience": "auditor", "route": "GET /audit/transactions/query", "top_level": True, "fields": 10, "required": 5, "note": "results is List[Dict[str, Any]], bypassing the declared TransactionOut contract"},
    "TransactionIntegrityReport": {"kind": "report", "audience": "auditor", "route": "GET /audit/transactions/integrity", "top_level": True, "fields": 11, "required": 1, "note": "checkpoints and sampled are untyped dicts; verdict is comment-only"},
    "TransactionExportReport": {"kind": "report", "audience": "auditor", "route": "GET /audit/transactions/export", "top_level": True, "fields": 10, "required": 5, "note": "the second content: str carrier, alongside a bytes count that describes it"},
}

#: The finding taxonomy. Every code here is emitted by one of the reports
#: below, which is asserted: a code whose row claims a producer that never emits
#: it is itself a finding, so the catalog reports the set of codes that no
#: report produced as ``unreached_codes`` rather than letting the taxonomy look
#: better than the implementation.
CONTRACT_WARNINGS: Dict[str, Dict[str, Any]] = {
    # --- inventory drift -----------------------------------------------------
    "CONTRACT_MODEL_UNDECLARED": {
        "severity": "defect",
        "emitted_by": ("contract_inventory",),
        "description": "a pydantic model this module defines has no CONTRACT_INVENTORY row",
        "remediation": "add the row; an undeclared contract has no declared audience and no reviewed route",
    },
    "CONTRACT_INVENTORY_STALE": {
        "severity": "defect",
        "emitted_by": ("contract_inventory",),
        "description": "a CONTRACT_INVENTORY row names a model the module no longer defines",
        "remediation": "remove the row, or restore the model -- a stale row is worse than none",
    },
    "CONTRACT_FIELD_COUNT_DRIFT": {
        "severity": "warning",
        "emitted_by": ("contract_inventory",),
        "description": "the declared field or required count no longer matches the model",
        "remediation": "update the row; the counts are what make a field added later visible",
    },
    "CONTRACT_KIND_MISMATCH": {
        "severity": "warning",
        "emitted_by": ("contract_inventory",),
        "description": "the model's name suffix implies a different kind than the row declares",
        "remediation": "align the row with the suffix, or rename the model",
    },
    "CONTRACT_AUDIENCE_UNKNOWN": {
        "severity": "warning",
        "emitted_by": ("contract_inventory", "validate_contracts"),
        "description": "a row names an audience that is not in CONTRACT_AUDIENCES",
        "remediation": "add the audience or correct the name; an unranked audience cannot be compared",
    },
    "CONTRACT_KIND_UNKNOWN": {
        "severity": "warning",
        "emitted_by": ("validate_contracts",),
        "description": "a row names a kind that is not in CONTRACT_KINDS, or declares a direction its kind forbids",
        "remediation": "add the kind or correct the name; a kind is what supplies the direction and the stamp expectation",
    },
    "CONTRACT_TABLE_MALFORMED": {
        "severity": "defect",
        "emitted_by": ("validate_contracts",),
        "description": "a governance table row is not a mapping, so it cannot be read",
        "remediation": "fix the row; every table here is a table of tables and a scalar in one is a typo",
    },
    "CONTRACT_WRITE_PATH_UNKNOWN": {
        "severity": "defect",
        "emitted_by": ("validate_contracts",),
        "description": "a CONTRACT_WRITE_PATHS row names a model this module does not define",
        "remediation": "correct the name; the row claims a route behaves a certain way about a contract that is not here",
    },
    # --- declaration quality -------------------------------------------------
    "CONTRACT_VOCABULARY_COMMENT_ONLY": {
        "severity": "defect",
        "emitted_by": ("contract_divergence_report",),
        "description": "a field lists its allowed values in a trailing comment and nothing enforces them",
        "remediation": "add a pattern=, or accept the field as a free label and say so in the row",
    },
    "CONTRACT_VOCABULARY_COLLISION": {
        "severity": "defect",
        "emitted_by": ("contract_divergence_report",),
        "description": "one field name carries two different vocabularies inside this module",
        "remediation": "rename one of them; a shared name with two meanings is a client bug waiting to happen",
    },
    "CONTRACT_FIELD_UNCAPED": {
        "severity": "defect",
        "emitted_by": ("contract_divergence_report",),
        "description": "a string on a request path has no max_length",
        "remediation": "cap it, or state in the policy row why the field is deliberately unbounded",
    },
    "CONTRACT_BLOB_UNBOUNDED": {
        "severity": "warning",
        "emitted_by": ("contract_divergence_report", "contract_redaction_report"),
        "description": "a free-form carrier declares no size, depth or item bound",
        "remediation": "declare the bound the writer enforces; the schema cannot enforce it",
    },
    "CONTRACT_TIMESTAMP_UNFORMATTED": {
        "severity": "defect",
        "emitted_by": ("contract_divergence_report",),
        "description": "a timestamp-shaped field is declared str, so no format is published",
        "remediation": "declare datetime, or document the string format in the field description",
    },
    "CONTRACT_LIST_ELEMENT_NULLABLE": {
        "severity": "warning",
        "emitted_by": ("contract_divergence_report",),
        "description": "a list is typed List[Optional[X]], so a null element validates",
        "remediation": "use List[X] and make the whole field Optional if absence is the real case",
    },
    "CONTRACT_TYPED_CONTRACT_BYPASSED": {
        "severity": "warning",
        "emitted_by": ("contract_divergence_report",),
        "description": (
            "a list is typed List[Dict[str, Any]] while a sibling contract in the same "
            "family types its list with a declared model. Warning, not defect: the "
            "schema does not say what the untyped field holds, so whether it is the same "
            "record is not decidable from the contract alone"
        ),
        "remediation": "declare the element type if the field does hold those records, or leave it untyped and say why",
    },
    "CONTRACT_ENVELOPE_MISSING_GENERATED_AT": {
        "severity": "warning",
        "emitted_by": ("contract_divergence_report",),
        "description": "a report or acknowledgement has no generated_at, so it cannot say how old it is",
        "remediation": "add the field, or record why the value is not a moment",
    },
    "CONTRACT_ENVELOPE_GENERATED_AT_OPTIONAL": {
        "severity": "info",
        "emitted_by": ("contract_divergence_report",),
        "description": "a report declares generated_at optional where its siblings require it",
        "remediation": "align it; a client cannot treat the stamp as present",
    },
    # --- asymmetry between paths ---------------------------------------------
    "CONTRACT_WRITE_ASYMMETRY": {
        "severity": "defect",
        "emitted_by": ("contract_divergence_report",),
        "description": "two write contracts for one table declare the same field but the paths behave differently",
        "remediation": "say which is which in the field description; the schema currently cannot",
    },
    "CONTRACT_WRITE_UNREDACTED": {
        "severity": "defect",
        "emitted_by": ("contract_redaction_report",),
        "description": (
            "a carrier write path stores its payload unscrubbed and the table row gives no "
            "reason. A row that states the exception does not fire: PipelineEventCreate"
            ".payload is deliberately unredacted because a dead-letter replay needs it intact"
        ),
        "remediation": "scrub it, or state in CONTRACT_WRITE_PATHS why the path is the exception",
    },
    "CONTRACT_RESPONSE_UNTYPED": {
        "severity": "warning",
        "emitted_by": ("contract_divergence_report",),
        "description": "a write route returns 2xx with no declared response schema, so a client cannot bind to it",
        "remediation": "annotate the return type; the other write routes in this module all do",
    },
    "CONTRACT_ROUTE_UNDECLARED": {
        "severity": "warning",
        "emitted_by": ("contract_inventory",),
        "description": "the route a contract is observed on differs from the one the inventory declares",
        "remediation": "update the row; the route is what a client binds to",
    },
    "CONTRACT_NESTED_ONLY_UNDECLARED": {
        "severity": "info",
        "emitted_by": ("contract_inventory",),
        "description": "top_level is declared False for a contract observed as a top-level response, or the reverse",
        "remediation": "update the row; this is what records that a model cannot be requested directly",
    },
    # --- redaction -----------------------------------------------------------
    "CONTRACT_FORBIDDEN_FIELD_PRESENT": {
        "severity": "defect",
        "emitted_by": ("contract_redaction_report",),
        "description": "a field name that must never be published is declared on a response contract",
        "remediation": "remove the field; a declared field is a published shape, which is not the same as a redacted carrier key",
    },
    "CONTRACT_REDACTION_ASYMMETRY": {
        "severity": "defect",
        "emitted_by": ("contract_redaction_report",),
        "description": (
            "two write paths declare the same carrier field identically and disagree about "
            "whether to scrub it, so the contract cannot tell a client which path is which"
        ),
        "remediation": (
            "declare the difference in the field description on both paths, or make them "
            "one contract; the service already flags the consequence with the "
            "no_unredacted_credentials gate"
        ),
    },
    "CONTRACT_REDACTION_UNDECLARED": {
        "severity": "warning",
        "emitted_by": ("contract_redaction_report",),
        "description": "the service redacts a carrier but the field declares no description saying so",
        "remediation": "add the description; the behaviour is correct and the contract is silent about it",
    },
    "CONTRACT_CARRIER_ON_RESPONSE": {
        "severity": "info",
        "emitted_by": ("contract_redaction_report",),
        "description": "a free-form carrier is published in a response contract, so its contents cross the boundary verbatim",
        "remediation": "none required; recorded so the redaction boundary is visible from the schema alone",
    },
    # --- table hygiene -------------------------------------------------------
    "CONTRACT_FIELD_UNDECLARED": {
        "severity": "info",
        "emitted_by": ("validate_contracts",),
        "description": "a field name carries no policy row, so nothing states who may read it",
        "remediation": "add a row for the names that recur; a one-off field does not need one",
    },
    "CONTRACT_POLICY_UNUSED": {
        "severity": "info",
        "emitted_by": ("validate_contracts",),
        "description": "a policy row matches no field, so it documents something that no longer exists",
        "remediation": "remove the row, or restore the field",
    },
    "CONTRACT_POLICY_CARRIER_MISMATCH": {
        "severity": "warning",
        "emitted_by": ("validate_contracts",),
        "description": "a policy row's declared carrier does not match the annotation of the fields it matches",
        "remediation": "correct the carrier; it is what the redaction report groups by",
    },
    "CONTRACT_FIELD_SHAPE_COLLISION": {
        "severity": "warning",
        "emitted_by": ("validate_contracts",),
        "description": (
            "one field name is declared with two different primitive types across the "
            "module, so the name alone does not tell a client what it holds. Container "
            "differences (list vs dict) are not reported; only a disagreement on a primitive"
        ),
        "remediation": "rename one of them, or accept the ambiguity and say so in the policy row",
    },
    "CONTRACT_OP_NOT_REPORT_ONLY": {
        "severity": "defect",
        "emitted_by": ("validate_contracts",),
        "description": "a CONTRACT_OPS row is not report_only, so the layer claims a power it does not have",
        "remediation": "set report_only; the layer is structurally incapable of editing a model",
    },
    "CONTRACT_TAXONOMY_NOT_EMITTED": {
        "severity": "info",
        "emitted_by": ("build_contract_catalog",),
        "description": "a taxonomy code no report produced, so the row describes a guard rather than a known finding",
        "remediation": "none; the code is a guard that only fires on a malformed table, and unreached is said out loud",
    },
    "CONTRACT_TAXONOMY_MALFORMED": {
        "severity": "defect",
        "emitted_by": ("validate_contracts",),
        "description": (
            "a CONTRACT_WARNINGS row is malformed: not a mapping, an unknown "
            "severity, a missing description, or an emitted_by that is not a "
            "sequence of real producer names"
        ),
        "remediation": (
            "fix the row. A string in emitted_by is the failure worth naming: it "
            "iterates one character at a time, so every consumer of that field "
            "silently degrades instead of failing"
        ),
    },
}

CONTRACT_GOVERNANCE_VERSION = "contract_governance_v1"


#: The models this layer defines for its own reports. Declared explicitly so
#: the inventory can exclude them by identity rather than by "everything after
#: the banner comment" -- an ordering rule would silently start treating a
#: future contract as governance, and silently stop treating a moved report as
#: a contract.
GOVERNANCE_MODELS: frozenset = frozenset(
    {
        "ContractFindingOut",
        "ContractModelOut",
        "ContractFieldOut",
        "ContractInventoryReport",
        "ContractDivergenceReport",
        "ContractRedactionReport",
        "ContractValidationReport",
        "ContractCatalogReport",
    }
)


# --- Governance reports ------------------------------------------------------
#
# New classes. Nothing above this point is renamed, retyped or given a new
# field, and no shipped route's payload changes shape: these describe contracts,
# they are not contracts clients bind to.


class ContractFindingOut(BaseModel):
    """One thing the governance layer noticed about a contract."""

    code: str
    severity: str
    producer: str
    subject: str
    detail: str
    evidence: Dict[str, Any] = Field(default_factory=dict)


class ContractModelOut(BaseModel):
    """A contract as the governance layer sees it."""

    model: str
    kind: str
    direction: str
    audience: str
    declared_route: str = ""
    observed_routes: List[str] = Field(default_factory=list)
    top_level: Optional[bool] = None
    fields: int = 0
    required: int = 0
    has_generated_at: bool = False
    generated_at_optional: bool = False
    required_fields: List[str] = Field(default_factory=list)
    field_names: List[str] = Field(default_factory=list)
    declared: bool = True
    note: str = ""


class ContractFieldOut(BaseModel):
    """The effective policy for one field, and where it is actually checked."""

    model: str
    field: str
    annotation: str
    required: Optional[bool] = None
    default_repr: Optional[str] = None
    description: Optional[str] = None
    kind: str
    audience: str
    carrier: Optional[str] = None
    vocabulary: Optional[List[str]] = None
    declared_in: Optional[str] = None
    comment_vocabulary: Optional[str] = None
    max_length: Optional[int] = None
    min_length: Optional[int] = None
    pattern: Optional[str] = None
    bounds: Dict[str, Any] = Field(default_factory=dict)
    redacted_on_write: Optional[bool] = None
    enforced_by: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    resolved: bool = True


class ContractInventoryReport(BaseModel):
    """The pinned surface, compared against the module."""

    generated_at: datetime
    version: str = CONTRACT_GOVERNANCE_VERSION
    models: int = 0
    declared: int = 0
    undeclared: List[str] = Field(default_factory=list)
    stale: List[str] = Field(default_factory=list)
    by_kind: Dict[str, int] = Field(default_factory=dict)
    by_audience: Dict[str, int] = Field(default_factory=dict)
    top_level: int = 0
    nested_only: int = 0
    route_observed: bool = False
    inventory: List[ContractModelOut] = Field(default_factory=list)
    findings: List[ContractFindingOut] = Field(default_factory=list)
    finding_count: int = 0


class ContractDivergenceReport(BaseModel):
    """Where the module contradicts itself."""

    generated_at: datetime
    version: str = CONTRACT_GOVERNANCE_VERSION
    models: int = 0
    fields_examined: int = 0
    checks: List[str] = Field(default_factory=list)
    comment_only_vocabularies: List[Dict[str, Any]] = Field(default_factory=list)
    vocabulary_collisions: List[Dict[str, Any]] = Field(default_factory=list)
    pattern_enforced: List[str] = Field(default_factory=list)
    uncaped_write_fields: List[Dict[str, Any]] = Field(default_factory=list)
    unbounded_blobs: List[Dict[str, Any]] = Field(default_factory=list)
    unformatted_timestamps: List[Dict[str, Any]] = Field(default_factory=list)
    nullable_element_lists: List[Dict[str, Any]] = Field(default_factory=list)
    bypassed_contracts: List[Dict[str, Any]] = Field(default_factory=list)
    envelopes_without_generated_at: List[str] = Field(default_factory=list)
    optional_generated_at: List[str] = Field(default_factory=list)
    write_asymmetries: List[Dict[str, Any]] = Field(default_factory=list)
    untyped_write_responses: List[str] = Field(default_factory=list)
    findings: List[ContractFindingOut] = Field(default_factory=list)
    finding_count: int = 0
    note: str = ""


class ContractRedactionReport(BaseModel):
    """Which carriers cross a trust boundary, and who scrubbed them."""

    generated_at: datetime
    version: str = CONTRACT_GOVERNANCE_VERSION
    carriers: List[Dict[str, Any]] = Field(default_factory=list)
    write_paths: List[Dict[str, Any]] = Field(default_factory=list)
    redacted_paths: int = 0
    unredacted_paths: int = 0
    forbidden_names: List[str] = Field(default_factory=list)
    forbidden_present: List[Dict[str, Any]] = Field(default_factory=list)
    carriers_on_responses: List[Dict[str, Any]] = Field(default_factory=list)
    findings: List[ContractFindingOut] = Field(default_factory=list)
    finding_count: int = 0
    boundary: str = ""
    note: str = ""


class ContractValidationReport(BaseModel):
    """Is this governance layer itself configured coherently."""

    generated_at: datetime
    version: str = CONTRACT_GOVERNANCE_VERSION
    ok: bool = True
    errors: int = 0
    warnings: int = 0
    info: int = 0
    counts: Dict[str, int] = Field(default_factory=dict)
    coded: Dict[str, List[str]] = Field(default_factory=dict)
    checks: Dict[str, Any] = Field(default_factory=dict)
    messages: List[str] = Field(default_factory=list)
    note: str = ""


class ContractCatalogReport(BaseModel):
    """Everything above, in one payload."""

    generated_at: datetime
    version: str = CONTRACT_GOVERNANCE_VERSION
    policy: Dict[str, Any] = Field(default_factory=dict)
    kinds: Dict[str, Any] = Field(default_factory=dict)
    audiences: Dict[str, Any] = Field(default_factory=dict)
    field_policies: Dict[str, Any] = Field(default_factory=dict)
    forbidden_fields: Dict[str, Any] = Field(default_factory=dict)
    ops: Dict[str, Any] = Field(default_factory=dict)
    findings_taxonomy: Dict[str, Any] = Field(default_factory=dict)
    inventory: ContractInventoryReport
    divergence: ContractDivergenceReport
    redaction: ContractRedactionReport
    validation: ContractValidationReport
    findings: List[ContractFindingOut] = Field(default_factory=list)
    finding_count: int = 0
    severity_counts: Dict[str, int] = Field(default_factory=dict)
    unreached_codes: List[str] = Field(default_factory=list)
    clean_reports: List[str] = Field(default_factory=list)
    ground_truth: Dict[str, Any] = Field(default_factory=dict)
    note: str = ""


# --- Governance functions ----------------------------------------------------


def _finding(
    code: str,
    producer: str,
    subject: str,
    detail: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a finding, taking its severity from the taxonomy.

    An unknown code is reported at ``error`` severity rather than raising: a
    code emitted without a taxonomy row is a defect in this layer, and the
    report is the place that says so, not the place that dies.
    """

    row = CONTRACT_WARNINGS.get(code) or {}
    return {
        "code": code,
        "severity": row.get("severity", "error"),
        "producer": producer,
        "subject": subject,
        "detail": detail,
        "evidence": dict(evidence or {}),
    }


def _kind_for(name: str) -> Optional[str]:
    """The kind a model's *name* implies, or None if the suffix is unknown.

    Longest suffix first. ``TransactionSpecReport`` ends in both ``SpecReport``
    and ``Report``, and a first-match-wins scan reports it as a plain report --
    which would make the declared ``spec`` kind look like a typo in the table
    rather than the fact it is.
    """

    best: Optional[str] = None
    best_len = 0
    for kind, row in CONTRACT_KINDS.items():
        for suffix in row.get("suffixes", ()):  # noqa: SIM118 -- the tuples are 1-2 long
            if len(suffix) > best_len and name.endswith(suffix):
                best, best_len = kind, len(suffix)
    return best


def _field_list(model: type) -> List[str]:
    try:
        return list(getattr(model, "model_fields", {}) or {})
    except Exception:  # noqa: BLE001 -- a malformed model must not break the sweep
        return []


def _route_map(routes: Optional[Any]) -> Dict[str, Any]:
    """Map model name -> routes, from an openapi document or a FastAPI app.

    Takes the document as an argument rather than importing ``app.main``:
    a schemas module that imported the app to inspect it would be a cycle, and
    the deps pass set the precedent. Without the argument the route-level checks
    report that they were skipped instead of quietly passing -- an unavailable
    check must never read as a clean one.
    """

    if routes is None:
        return {"available": False, "models": {}, "reason": "no openapi document supplied"}
    spec: Any = routes
    try:
        if hasattr(routes, "openapi"):
            spec = routes.openapi()
        if not isinstance(spec, dict) or "paths" not in spec:
            return {"available": False, "models": {}, "reason": "not an openapi document"}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "models": {}, "reason": f"{type(exc).__name__}: {exc}"}

    names = set(_audit_models())

    def collect(node: Any, acc: set) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                acc.add(ref.rsplit("/", 1)[-1])
                return
            for value in node.values():
                collect(value, acc)
        elif isinstance(node, list):
            for value in node:
                collect(value, acc)

    models: Dict[str, Dict[str, List[str]]] = {}
    for path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                continue
            label = f"{method.upper()} {path}"
            body: set = set()
            collect(op.get("requestBody") or {}, body)
            for name in body & names:
                models.setdefault(name, {"body": [], "response": [], "response_paths": []})["body"].append(label)
            for code, response in (op.get("responses") or {}).items():
                if not str(code).startswith("2"):
                    continue
                found: set = set()
                collect(response, found)
                for name in found & names:
                    entry = models.setdefault(
                        name, {"body": [], "response": [], "response_paths": []}
                    )
                    entry["response"].append(f"{label} [{code}]")
                    # The bare method+path, kept separately: the inventory
                    # declares a route, not a route plus a status code, so
                    # matching "GET /audit/logs" against "GET /audit/logs [200]"
                    # would declare every response route mismatched.
                    entry["response_paths"].append(label)
    return {"available": True, "models": models, "reason": ""}


def contract_inventory(routes: Optional[Any] = None) -> ContractInventoryReport:
    """Every contract this module defines, checked against the pinned table.

    The comparison is the point. A module of 40 models with no inventory is 40
    contracts whose audience, direction and route are known only to whoever
    last read the router.
    """

    findings: List[Dict[str, Any]] = []
    models = _audit_models()
    observed = _route_map(routes)
    rows: List[ContractModelOut] = []

    for name, model in models.items():
        row = CONTRACT_INVENTORY.get(name)
        fields = _field_list(model)
        required = []
        for fname in fields:
            try:
                if model.model_fields[fname].is_required():
                    required.append(fname)
            except Exception:  # noqa: BLE001
                pass

        if not isinstance(row, dict):
            findings.append(
                _finding(
                    "CONTRACT_MODEL_UNDECLARED",
                    "contract_inventory",
                    name,
                    "the module defines this model and CONTRACT_INVENTORY does not describe it",
                    {"fields": len(fields), "required": len(required)},
                )
            )
            kind = _kind_for(name) or "out"
            rows.append(
                ContractModelOut(
                    model=name,
                    kind=kind,
                    direction=CONTRACT_KINDS.get(kind, {}).get("direction", "out"),
                    audience="machine_client",
                    declared_route="",
                    fields=len(fields),
                    required=len(required),
                    required_fields=required,
                    field_names=fields,
                    declared=False,
                    note="undeclared in CONTRACT_INVENTORY",
                )
            )
            continue

        kind = row.get("kind") or "out"
        kind_row = CONTRACT_KINDS.get(kind) or {}
        audience = row.get("audience") or "machine_client"
        if audience not in CONTRACT_AUDIENCES:
            findings.append(
                _finding(
                    "CONTRACT_AUDIENCE_UNKNOWN",
                    "contract_inventory",
                    f"{name} ({audience})",
                    "the inventory row names an audience that CONTRACT_AUDIENCES does not define",
                    {"audience": audience},
                )
            )

        implied = _kind_for(name)
        if implied and implied != kind:
            findings.append(
                _finding(
                    "CONTRACT_KIND_MISMATCH",
                    "contract_inventory",
                    name,
                    f"the name ends in {implied!r} but the row declares kind={kind!r}",
                    {"implied": implied, "declared": kind},
                )
            )

        for declared_key, actual in (("fields", len(fields)), ("required", len(required))):
            if row.get(declared_key) is not None and row.get(declared_key) != actual:
                findings.append(
                    _finding(
                        "CONTRACT_FIELD_COUNT_DRIFT",
                        "contract_inventory",
                        f"{name}.{declared_key}",
                        f"the inventory declares {row.get(declared_key)} and the model has {actual}",
                        {"declared": row.get(declared_key), "actual": actual},
                    )
                )

        seen = observed["models"].get(name) if observed["available"] else None
        observed_routes = sorted(set((seen or {}).get("body", []) + (seen or {}).get("response", [])))
        if observed["available"] and seen is not None:
            declared_route = str(row.get("route") or "")
            # Accept a match on either the bare route or the status-qualified
            # one, so a row may declare "GET /audit/logs" or
            # "GET /audit/logs [200]" without tripping the check.
            known = set(observed_routes) | set((seen or {}).get("response_paths", []))
            if declared_route and declared_route not in known:
                findings.append(
                    _finding(
                        "CONTRACT_ROUTE_UNDECLARED",
                        "contract_inventory",
                        name,
                        f"the inventory declares {declared_route!r} and the document does not show it",
                        {"declared": declared_route, "observed": observed_routes},
                    )
                )
            # ``top_level`` means "can be a top-level *response*", so a request
            # body does not count. Testing against every observed label would
            # call all 4 Create models top-level and contradict the table.
            is_top = bool((seen or {}).get("response"))
            if bool(row.get("top_level")) != is_top:
                findings.append(
                    _finding(
                        "CONTRACT_NESTED_ONLY_UNDECLARED",
                        "contract_inventory",
                        name,
                        "top_level disagrees with what the openapi document shows",
                        {
                            "declared_top_level": row.get("top_level"),
                            "observed_responses": sorted((seen or {}).get("response", [])),
                        },
                    )
                )

        rows.append(
            ContractModelOut(
                model=name,
                kind=kind,
                direction=str(kind_row.get("direction", "out")),
                audience=audience,
                declared_route=str(row.get("route") or ""),
                observed_routes=observed_routes,
                top_level=row.get("top_level") if isinstance(row.get("top_level"), bool) else None,
                fields=len(fields),
                required=len(required),
                has_generated_at="generated_at" in fields,
                generated_at_optional="generated_at" in fields
                and "generated_at" not in required,
                required_fields=required,
                field_names=fields,
                declared=True,
                note=str(row.get("note") or ""),
            )
        )

    for name in CONTRACT_INVENTORY:
        if name not in models:
            findings.append(
                _finding(
                    "CONTRACT_INVENTORY_STALE",
                    "contract_inventory",
                    name,
                    "CONTRACT_INVENTORY describes a model this module no longer defines",
                    {},
                )
            )

    by_kind: Dict[str, int] = {}
    by_audience: Dict[str, int] = {}
    for row in rows:
        by_kind[row.kind] = by_kind.get(row.kind, 0) + 1
        by_audience[row.audience] = by_audience.get(row.audience, 0) + 1

    return ContractInventoryReport(
        generated_at=datetime.now(),
        models=len(models),
        declared=sum(1 for row in rows if row.declared),
        undeclared=sorted(n for n in models if n not in CONTRACT_INVENTORY),
        stale=sorted(n for n in CONTRACT_INVENTORY if n not in models),
        by_kind=dict(sorted(by_kind.items())),
        by_audience=dict(sorted(by_audience.items())),
        top_level=sum(1 for row in rows if row.top_level),
        # A request body is not top-level either, so counting "not top_level"
        # would put the 4 `create` contracts in with the 14 models that can only
        # ever appear inside another contract. They are different facts -- one is
        # bound to a request, the other is unreachable on its own -- so the
        # count excludes `create` and the three numbers partition the 40.
        nested_only=sum(
            1 for row in rows if row.top_level is False and row.kind != "create"
        ),
        route_observed=bool(observed["available"]),
        inventory=rows,
        findings=findings,
        finding_count=len(findings),
    )


def _vocab_of(text: Optional[str]) -> List[str]:
    """``"a | b | c"`` -> ``["a", "b", "c"]``. Tolerant by design."""

    if not isinstance(text, str) or "|" not in text:
        return []
    return [part.strip() for part in text.split("|") if part.strip()]


def _shape_of(annotation: Any) -> Dict[str, Any]:
    """Structural facts about an annotation, read from the object.

    Deliberately *not* a string match against ``repr(annotation)``. That repr is
    a ``typing`` rendering -- ``List[Dict[str, Any]]`` comes out as
    ``typing.List[typing.Dict[str, typing.Any]]`` and ``Optional[str]`` as
    ``typing.Optional[str]`` -- so a check written against the pretty form
    silently matches nothing and reports clean. Four checks were wrong that way
    before this existed; the facts are now read from the type.

    Returns a dict rather than raising on an annotation it does not recognise:
    ``known: False`` is the answer, not an exception.
    """

    out: Dict[str, Any] = {
        "known": False,
        "is_list": False,
        "is_dict": False,
        "is_optional": False,
        "is_str": False,
        "is_datetime": False,
        "is_any": False,
        "is_bool": False,
        "is_int": False,
        "is_dict_str_any": False,
        "element": None,
        "element_is_optional": False,
        "element_is_dict_str_any": False,
    }
    try:
        import typing

        args = typing.get_args(annotation)
        # Unwrap Optional only. An earlier version unwrapped *any* single-arg
        # generic, which silently turned List[Dict[str, Any]] into a bare dict
        # and made the bypassed-contract check match nothing -- a check that
        # reports clean because it stopped looking.
        if type(None) in args:
            out["is_optional"] = True
            annotation = next(a for a in args if a is not type(None))

        out["is_list"] = getattr(annotation, "__origin__", None) is list or annotation is list
        out["is_dict"] = getattr(annotation, "__origin__", None) is dict or annotation is dict
        out["is_str"] = annotation is str
        out["is_datetime"] = annotation is datetime
        out["is_bool"] = annotation is bool
        out["is_int"] = annotation is int
        out["is_any"] = annotation is Any
        if out["is_dict"]:
            out["is_dict_str_any"] = typing.get_args(annotation) == (str, Any)

        if out["is_list"]:
            element_args = typing.get_args(annotation)
            element = element_args[0] if element_args else None
            out["element"] = element
            out["element_is_optional"] = type(None) in typing.get_args(element)
            # The element must be tested as a whole: for List[Dict[str, Any]]
            # the element's own args are (str, Any), so unpacking them and
            # asking whether str is a dict -- the previous attempt -- is always
            # False and the check never fires.
            out["element_is_dict_str_any"] = (
                getattr(element, "__origin__", None) is dict
                and typing.get_args(element) == (str, Any)
            )
        # `known` means *recognised*, not "did not raise". Setting it
        # unconditionally made _shape_of(None) claim it understood an annotation
        # it had no fact about, which is the same failure the docstring above
        # warns against -- a check that reports what it did not look at.
        out["known"] = any(
            out[flag]
            for flag in (
                "is_list",
                "is_dict",
                "is_str",
                "is_datetime",
                "is_bool",
                "is_int",
                "is_any",
            )
        )
    except Exception as exc:  # noqa: BLE001 -- an odd annotation is a fact, not a crash
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _shape_key(shape: Dict[str, Any]) -> str:
    """A short, comparable name for an annotation's shape.

    Prefixed by container so the collision check can tell "a list where another
    contract has a dict" (a container choice) from "a bool where another has an
    int" (a different meaning).
    """

    if shape.get("is_list"):
        element = shape.get("element")
        if shape.get("element_is_dict_str_any"):
            return "list:dict[str,Any]"
        if shape.get("element_is_optional"):
            return "list:optional"
        if element is str:
            return "list:str"
        if element is int:
            return "list:int"
        if element is datetime:
            return "list:datetime"
        return f"list:{getattr(element, '__name__', 'unknown')}"
    if shape.get("is_dict_str_any"):
        return "dict:str,Any"
    if shape.get("is_dict"):
        return "dict"
    if shape.get("is_any"):
        return "any"
    if shape.get("is_str"):
        return "str"
    if shape.get("is_datetime"):
        return "datetime"
    if shape.get("is_bool"):
        return "bool"
    if shape.get("is_int"):
        return "int"
    return "unknown"


def _actual_carrier(shape: Dict[str, Any], field: str) -> str:
    """The carrier class a field really belongs to, from its annotation.

    ``embedded`` is a str that carries a document rather than a label. It is
    only reachable from a policy row, never inferred here: an annotation cannot
    tell you that ``content: str`` holds a serialised export, and a heuristic
    that guessed would eventually call some ordinary string embedded too.
    """

    if shape.get("is_list"):
        return "list"
    if shape.get("is_dict_str_any") or shape.get("element_is_dict_str_any"):
        return "blob"
    if field.endswith(("_at", "_on", "_date")) or shape.get("is_datetime"):
        return "timestamp"
    if shape.get("is_str"):
        return "scalar"
    return ""


def _typed_list_sibling(models: Dict[str, type], model_name: str) -> Optional[Dict[str, str]]:
    """Find where this module *does* type a list, for the same kind of record.

    A contract is only bypassed when something declared exists to bypass. The
    search is for a list field elsewhere in the module whose element is a model
    declared here, restricted to the same leading family token -- a dead-letter
    entry is never offered as the contract for a transaction frame. The family
    is the leading capitalised token, not everything before "Report":
    TransactionQueryReport and TransactionLogReport are both ``Transaction``
    contracts, and the narrower split hid the case it was written to find.
    Returns None when the module declares no such element, which is the honest
    answer for a filters list.
    """

    import re

    family_match = re.match(r"[A-Z][a-z]*", model_name)
    family = family_match.group(0) if family_match else model_name
    for other_name, other in models.items():
        if other_name == model_name or not other_name.startswith(family):
            continue
        for field in _field_list(other):
            try:
                annotation = other.model_fields[field].annotation
            except Exception:  # noqa: BLE001
                continue
            shape = _shape_of(annotation)
            if not shape.get("is_list"):
                continue
            element = shape.get("element")
            if isinstance(element, type) and issubclass(element, BaseModel):
                return {"model": element.__name__, "field": f"{other_name}.{field}"}
    return None


def _carrier_of(annotation: str, field: str = "") -> str:
    """Classify a field into the buckets the policies use.

    The *name* is consulted as well as the annotation, because a timestamp
    declared ``str`` is exactly the case worth classifying correctly: on the
    annotation alone ``Optional[str]`` reads as a scalar, and the one field in
    this module whose format is unpublished would be filed as a free label.
    """

    text = (annotation or "").strip()
    name = (field or "").strip()
    if name.endswith(("_at", "_on", "_date")) or "datetime" in text:
        return "timestamp"
    if text.startswith("List[") or text.startswith("list["):
        return "list"
    if text.startswith("Dict[") or text.startswith("dict[") or "Dict[str, Any]" in text:
        return "blob"
    if text in ("str", "Optional[str]", "<class 'str'>"):
        return "scalar"
    return "other"


def describe_contract_field(model: str, field: str) -> ContractFieldOut:
    """The effective policy for one field, and where it is actually checked.

    Answers the question a schema file cannot: *if I send something wrong here,
    what stops it?* The answer is ``enforced_by``, and it is frequently "the
    comment next to it". An unresolvable field comes back with
    ``resolved: False`` and the reason in ``notes`` rather than raising -- a
    description endpoint that 500s on a typo is not a description endpoint.
    """

    models = _audit_models()
    notes: List[str] = []
    target = models.get(model)
    if target is None:
        return ContractFieldOut(
            model=model,
            field=field,
            annotation="unknown",
            kind="unknown",
            audience="machine_client",
            resolved=False,
            notes=[f"{model!r} is not a contract this module defines"],
        )

    facts = _field_facts(target, field)
    if not facts["resolved"]:
        return ContractFieldOut(
            model=model,
            field=field,
            annotation="unknown",
            kind="unknown",
            audience="machine_client",
            resolved=False,
            notes=[str(facts.get("error") or f"{model} has no field {field!r}")],
        )

    annotation = str(facts.get("annotation_full") or facts.get("annotation") or "")
    kind_row = CONTRACT_INVENTORY.get(model) or {}
    kind = str(kind_row.get("kind") or _kind_for(model) or "out")
    audience = str(kind_row.get("audience") or "machine_client")
    policy = CONTRACT_FIELD_POLICIES.get(field) or {}
    comment = _source_vocabularies().get(f"{model}.{field}")
    vocab = list(policy.get("vocabulary") or []) or _vocab_of(comment)

    enforced: List[str] = []
    # Enforcement is *derived from the field*, never read from the policy row.
    # A row that claims `pattern` while the field has none is exactly the
    # situation this function exists to surface, so taking the claim at face
    # value would report the defect as clean.
    if facts.get("pattern"):
        enforced.append(f"pattern={facts['pattern']!r} on the field")
    elif comment:
        enforced.append(
            "nothing; the vocabulary is a trailing comment only "
            f"({comment.replace('|', '/')}), so any string validates"
        )
    elif policy.get("declared_in") == "column_check":
        enforced.append("a database CHECK constraint (declared, not verified here)")
    elif _carrier_of(annotation, field) == "timestamp":
        enforced.append("nothing; the field is a timestamp and no format is published for it")
    else:
        enforced.append("nothing; the value is a free label by design")

    if policy.get("vocabulary") and comment and _vocab_of(comment) != list(policy["vocabulary"]):
        notes.append(
            f"the policy row says {list(policy['vocabulary'])} and the source comment says "
            f"{_vocab_of(comment)}"
        )
    if policy.get("declared_in") == "pattern" and not facts.get("pattern"):
        notes.append(
            "CONTRACT_FIELD_POLICIES declares this vocabulary as pattern-enforced, but the "
            "field carries no pattern"
        )
    if facts.get("description"):
        notes.append("described: " + str(facts["description"])[:200])

    return ContractFieldOut(
        model=model,
        field=field,
        annotation=annotation,
        required=facts.get("required"),
        default_repr=facts.get("default_repr"),
        description=facts.get("description"),
        kind=kind,
        audience=audience,
        carrier=str(policy.get("carrier") or _carrier_of(annotation, field)),
        vocabulary=vocab or None,
        declared_in=str(policy.get("declared_in") or ""),
        comment_vocabulary=comment,
        max_length=facts.get("max_length"),
        min_length=facts.get("min_length"),
        pattern=facts.get("pattern"),
        bounds={
            "ge": facts.get("ge"),
            "le": facts.get("le"),
            "max_length": facts.get("max_length"),
            "min_length": facts.get("min_length"),
        },
        enforced_by=enforced,
        notes=notes,
        resolved=True,
    )


def contract_divergence_report(routes: Optional[Any] = None) -> ContractDivergenceReport:
    """Every way the module contradicts itself, checked structurally.

    Nine independent checks, each derived from the classes rather than from a
    hand-written list -- so a check cannot quietly stop covering a field because
    somebody edited a comment elsewhere. The checks are listed in ``checks`` so
    a reader can see what ran, including the two that need ``routes=`` and
    report themselves as skipped when it was not supplied.
    """

    models = _audit_models()
    vocabularies = _source_vocabularies()
    observed = _route_map(routes)
    findings: List[Dict[str, Any]] = []

    comment_only: List[Dict[str, Any]] = []
    pattern_enforced: List[str] = []
    by_name: Dict[str, Dict[str, Any]] = {}
    uncaped: List[Dict[str, Any]] = []
    blobs: List[Dict[str, Any]] = []
    unformatted: List[Dict[str, Any]] = []
    nullable_lists: List[Dict[str, Any]] = []
    bypassed: List[Dict[str, Any]] = []
    fields_examined = 0

    for model_name, model in models.items():
        kind_row = CONTRACT_INVENTORY.get(model_name) or {}
        is_write = str(kind_row.get("kind") or _kind_for(model_name) or "out") == "create"
        for field in _field_list(model):
            fields_examined += 1
            facts = _field_facts(model, field)
            if not facts["resolved"]:
                continue
            shape = _shape_of(
                (getattr(model.model_fields[field], "annotation", None))
                if field in getattr(model, "model_fields", {})
                else None
            )
            short = str(facts.get("annotation") or "")
            comment = vocabularies.get(f"{model_name}.{field}")
            is_str = bool(shape.get("is_str"))
            is_list = bool(shape.get("is_list"))

            # -- 1. vocabulary: pattern-enforced vs comment-only -------------
            # Both sources feed the collision index. A name whose vocabularies
            # differ is only visible if the pattern-declared values are in the
            # same bucket as the comment-declared ones -- and severity is
            # exactly that case, with three fields pattern-enforced on
            # info|warning|critical and one comment-only on advisory|review|
            # reject under the same name.
            if comment:
                by_name.setdefault(field, {"vocabularies": {}, "models": {}})
                by_name[field]["vocabularies"].setdefault(comment, []).append(
                    f"{model_name}.{field}"
                )
                by_name[field]["models"].setdefault(comment, []).append(model_name)
            if facts.get("pattern"):
                pattern_enforced.append(f"{model_name}.{field}")
                declared = _vocab_of(facts["pattern"].strip("^$").replace("(", "").replace(")", "").replace("|", "|"))
                if declared:
                    by_name.setdefault(field, {"vocabularies": {}, "models": {}})
                    by_name[field]["vocabularies"].setdefault(
                        " | ".join(declared), []
                    ).append(f"{model_name}.{field} (pattern)")
                continue
            if comment and is_str:
                comment_only.append(
                    {
                        "subject": f"{model_name}.{field}",
                        "values": _vocab_of(comment),
                        "default": facts.get("default_repr"),
                        "accepts_anything": True,
                    }
                )
                findings.append(
                    _finding(
                        "CONTRACT_VOCABULARY_COMMENT_ONLY",
                        "contract_divergence_report",
                        f"{model_name}.{field}",
                        f"the allowed values are a trailing comment ({comment}) and any string validates",
                        {"values": _vocab_of(comment), "default": facts.get("default_repr")},
                    )
                )

            # -- 2. uncapped strings on a write path -------------------------
            if is_write and is_str and facts.get("max_length") is None and not facts.get("pattern"):
                uncaped.append(
                    {
                        "subject": f"{model_name}.{field}",
                        "min_length": facts.get("min_length"),
                        "max_length": None,
                        "required": facts.get("required"),
                    }
                )
                findings.append(
                    _finding(
                        "CONTRACT_FIELD_UNCAPED",
                        "contract_divergence_report",
                        f"{model_name}.{field}",
                        "a string on a request path with no max_length, while every sibling is capped",
                        {"min_length": facts.get("min_length"), "required": facts.get("required")},
                    )
                )

            # -- 3. unbounded carriers ---------------------------------------
            # Only Dict[str, Any] is a free-form carrier. Dict[str, int] is a
            # counter map with a fixed shape and calling it unbounded would
            # bury the 6 real ones under 24 counts.
            policy = CONTRACT_FIELD_POLICIES.get(field) or {}
            free_form = bool(shape.get("is_dict_str_any")) or bool(shape.get("element_is_dict_str_any"))
            if str(policy.get("carrier")) == "blob" or free_form:
                blobs.append(
                    {
                        "subject": f"{model_name}.{field}",
                        "direction": "in" if is_write else "out",
                        "declared_bound": None,
                    }
                )
                findings.append(
                    _finding(
                        "CONTRACT_BLOB_UNBOUNDED",
                        "contract_divergence_report",
                        f"{model_name}.{field}",
                        "a free-form carrier with no size, depth or item bound declared in the schema",
                        {
                            "direction": "in" if is_write else "out",
                            "annotation": short,
                            "is_list": is_list,
                        },
                    )
                )

            # -- 4. timestamps published as strings -------------------------
            timestamp_named = field.endswith(("_at", "_on", "_date"))
            if timestamp_named and is_str:
                unformatted.append(
                    {
                        "subject": f"{model_name}.{field}",
                        "annotation": short,
                        "optional": bool(shape.get("is_optional")),
                        "siblings_as_datetime": 0,
                    }
                )
                findings.append(
                    _finding(
                        "CONTRACT_TIMESTAMP_UNFORMATTED",
                        "contract_divergence_report",
                        f"{model_name}.{field}",
                        "a timestamp-shaped field declared as str, so no format reaches the client",
                        {"annotation": short, "optional": bool(shape.get("is_optional"))},
                    )
                )

            # -- 5. lists whose element may be null -------------------------
            if is_list and shape.get("element_is_optional"):
                nullable_lists.append(
                    {"subject": f"{model_name}.{field}", "annotation": short}
                )
                findings.append(
                    _finding(
                        "CONTRACT_LIST_ELEMENT_NULLABLE",
                        "contract_divergence_report",
                        f"{model_name}.{field}",
                        "the element type admits None, so a list containing a null validates",
                        {"annotation": short},
                    )
                )

            # -- 6. a declared contract bypassed by Dict[str, Any] -----------
            # Only a finding when the module *does* declare a model for the
            # element and types it somewhere else. An untyped list with no
            # declared counterpart bypasses nothing: TransactionQueryReport
            # .filters is a repeated parameter descriptor, and calling that a
            # bypass would bury the one real case -- .results, where
            # TransactionLogReport already types the same frames as
            # List[TransactionOut].
            #
            # A name the trivial table covers cannot be a list of records, so
            # it is skipped on the name alone. Without that the family search
            # matched `TransactionLogReport.tail` for `filters` and produced
            # four cases of which three were parameter and checkpoint lists --
            # the comment above described the intended behaviour and the code
            # did the opposite.
            if (
                is_list
                and shape.get("element_is_dict_str_any")
                and field not in CONTRACT_TRIVIAL_FIELDS
            ):
                typed_twin = _typed_list_sibling(models, model_name)
                entry = {
                    "subject": f"{model_name}.{field}",
                    "declared_model": typed_twin.get("model") if typed_twin else None,
                    "typed_at": typed_twin.get("field") if typed_twin else None,
                    # the full annotation, not `short`: for a List field the
                    # short form is the bare origin "List", which distinguishes
                    # nothing and made this entry indistinguishable from a typed
                    # one in the payload
                    "annotation": str(facts.get("annotation_full") or short),
                }
                bypassed.append(entry)
                if typed_twin:
                    findings.append(
                        _finding(
                            "CONTRACT_TYPED_CONTRACT_BYPASSED",
                            "contract_divergence_report",
                            f"{model_name}.{field}",
                            f"List[Dict[str, Any]] here, while {typed_twin['field']} "
                            f"declares List[{typed_twin['model']}] for the same family",
                            {
                                "annotation": short,
                                "declared_model": typed_twin["model"],
                                "typed_at": typed_twin["field"],
                                "caveat": (
                                    "the schema does not say what this field holds, so whether "
                                    "it is the same record is not decidable from the contract"
                                ),
                            },
                        )
                    )

    # sibling counts for the unformatted timestamps, computed after the sweep
    for entry in unformatted:
        family = entry["subject"].split(".")[0]
        entry["siblings_as_datetime"] = sum(
            1
            for other in models
            if other.startswith(family)
            for f in _field_list(models[other])
            if f.endswith(("_at", "_on", "_date"))
            and _field_facts(models[other], f).get("annotation") != "str"
        )

    # -- 7. one name, two vocabularies --------------------------------------
    collisions: List[Dict[str, Any]] = []
    for field, slot in sorted(by_name.items()):
        if len(slot["vocabularies"]) < 2:
            continue
        collisions.append(
            {
                "field": field,
                "vocabularies": {
                    text: sorted(models_) for text, models_ in sorted(slot["vocabularies"].items())
                },
            }
        )
        findings.append(
            _finding(
                "CONTRACT_VOCABULARY_COLLISION",
                "contract_divergence_report",
                field,
                f"{field!r} carries {len(slot['vocabularies'])} different vocabularies in one module",
                {"vocabularies": {t: sorted(m) for t, m in sorted(slot["vocabularies"].items())}},
            )
        )

    # -- 8. envelopes and their age stamp ------------------------------------
    missing_stamp: List[str] = []
    optional_stamp: List[str] = []
    for model_name, model in models.items():
        kind = str((CONTRACT_INVENTORY.get(model_name) or {}).get("kind") or _kind_for(model_name) or "")
        if kind not in ("report", "accepted"):
            continue
        if "generated_at" not in _field_list(model):
            missing_stamp.append(model_name)
            findings.append(
                _finding(
                    "CONTRACT_ENVELOPE_MISSING_GENERATED_AT",
                    "contract_divergence_report",
                    model_name,
                    "an envelope with no generated_at, so a client cannot tell how old it is",
                    {"kind": kind},
                )
            )
        elif model.model_fields["generated_at"].is_required() is False:
            optional_stamp.append(model_name)
            findings.append(
                _finding(
                    "CONTRACT_ENVELOPE_GENERATED_AT_OPTIONAL",
                    "contract_divergence_report",
                    f"{model_name}.generated_at",
                    "generated_at is Optional where the sibling envelopes require it",
                    {"kind": kind},
                )
            )

    # -- 9. the two write contracts for one table ----------------------------
    writes = [
        name
        for name in models
        if str((CONTRACT_INVENTORY.get(name) or {}).get("kind")) == "create"
    ]
    asymmetries: List[Dict[str, Any]] = []
    for i, left in enumerate(writes):
        for right in writes[i + 1 :]:
            shared = set(_field_list(models[left])) & set(_field_list(models[right]))
            for field in sorted(shared):
                a = _field_facts(models[left], field)
                b = _field_facts(models[right], field)
                same = (
                    a.get("annotation_full") == b.get("annotation_full")
                    and a.get("pattern") == b.get("pattern")
                    and a.get("max_length") == b.get("max_length")
                    and a.get("default_repr") == b.get("default_repr")
                )
                if same:
                    continue
                asymmetries.append(
                    {
                        "left": left,
                        "right": right,
                        "field": field,
                        "left_default": a.get("default_repr"),
                        "right_default": b.get("default_repr"),
                        "left_annotation": a.get("annotation_full"),
                        "right_annotation": b.get("annotation_full"),
                    }
                )
                findings.append(
                    _finding(
                        "CONTRACT_WRITE_ASYMMETRY",
                        "contract_divergence_report",
                        f"{left}.{field} vs {right}.{field}",
                        "two write contracts declare the same field differently",
                        {
                            "left_default": a.get("default_repr"),
                            "right_default": b.get("default_repr"),
                        },
                    )
                )

    # -- 10. write routes with no declared response -------------------------
    # Keyed on the *route*, not on the model. A write route's response is
    # usually a different contract (POST /audit/log/auditable answers with
    # AuditLogEntryOut, not with AuditableLogEntryCreate), so asking "does this
    # Create model have a response of its own" reports every write route as
    # untyped -- the opposite of the truth.
    untyped_writes: List[str] = []
    checks = [
        "vocabulary_enforcement",
        "uncapped_write_strings",
        "unbounded_carriers",
        "unformatted_timestamps",
        "nullable_element_lists",
        "bypassed_contracts",
        "vocabulary_collisions",
        "envelope_stamps",
        "write_asymmetry",
    ]
    if observed["available"]:
        checks.append("untyped_write_responses")
        write_routes: Dict[str, List[str]] = {}
        for name in writes:
            for label in (observed["models"].get(name) or {}).get("body", []):
                write_routes.setdefault(label, []).append(name)
        for label in sorted(write_routes):
            declared_responses: set = set()
            for name in models:
                declared_responses.update(
                    (observed["models"].get(name) or {}).get("response_paths", [])
                )
            if label in declared_responses:
                continue
            untyped_writes.append(label)
            findings.append(
                _finding(
                    "CONTRACT_RESPONSE_UNTYPED",
                    "contract_divergence_report",
                    label,
                    "a write route whose 2xx response declares no schema, so a client cannot bind to it",
                    {"body": sorted(write_routes[label])},
                )
            )
    else:
        checks.append("untyped_write_responses:skipped")

    return ContractDivergenceReport(
        generated_at=datetime.now(),
        models=len(models),
        fields_examined=fields_examined,
        checks=checks,
        comment_only_vocabularies=comment_only,
        vocabulary_collisions=collisions,
        pattern_enforced=sorted(pattern_enforced),
        uncaped_write_fields=uncaped,
        unbounded_blobs=blobs,
        unformatted_timestamps=unformatted,
        nullable_element_lists=nullable_lists,
        bypassed_contracts=bypassed,
        envelopes_without_generated_at=sorted(missing_stamp),
        optional_generated_at=sorted(optional_stamp),
        write_asymmetries=asymmetries,
        untyped_write_responses=untyped_writes,
        findings=findings,
        finding_count=len(findings),
        note=(
            "every check is derived from the module's own classes, so editing a "
            "comment cannot silently narrow what is covered; the two checks that "
            "need the openapi document report themselves as skipped without routes="
        ),
    )


#: The write paths that carry a free-form detail blob, and whether the service
#: scrubs it. This is the one fact in the module a schema cannot state, because
#: the two audit paths declare the field identically:
#:
#: ``AuditLogEntryCreate.detail``     ``Dict[str, Any]``, no description. The
#:     ``/audit/log`` handler passes it to ``record_audit_log_entry``, which
#:     hands it straight to ``_detail_json`` -- stored verbatim, no redaction,
#:     no action-catalog check, no justification requirement, no seal.
#: ``AuditableLogEntryCreate.detail`` the same type, the same silence, on
#:     ``/audit/log/auditable``, which routes to ``record_auditable`` and *does*
#:     run ``redact_detail``.
#:
#: A client reading the OpenAPI document sees two identical ``Dict[str, Any]``
#: fields and has no way to learn that one of them is scrubbed. That is the
#: headline finding of this module, and it is unfixable from the schema: adding
#: a description to one field is the cheap half, and even then a reader is
#: comparing two identical types. The service already knows -- the
#: ``no_unredacted_credentials`` integrity gate exists to catch the entries the
#: ungated path leaves behind.
#:
#: The paths are declared here rather than imported from the service, because a
#: schemas module that imported the service to ask it about itself would be a
#: layering inversion, and because the fact is worth stating in one place that
#: can be read next to the two field declarations. It is asserted against the
#: service in the tests, so the table cannot quietly go stale.
CONTRACT_WRITE_PATHS: Dict[str, Dict[str, Any]] = {
    "POST /audit/log": {
        "model": "AuditLogEntryCreate",
        "carrier": "detail",
        "service": "record_audit_log_entry",
        "redacts": False,
        "returns": None,
        "note": (
            "the raw write: detail reaches _detail_json untouched. The 201 has no "
            "declared response schema either, so this route publishes no contract "
            "at all -- neither the scrubbed shape nor the unscrubbed one"
        ),
    },
    "POST /audit/log/auditable": {
        "model": "AuditableLogEntryCreate",
        "carrier": "detail",
        "service": "record_auditable",
        "redacts": True,
        "returns": "AuditLogEntryOut",
        "note": (
            "the governed write: redact_detail runs, strict aliases the action "
            "catalog, require_justification is enforced and the seal is stamped"
        ),
    },
    "POST /audit/pipeline/event": {
        "model": "PipelineEventCreate",
        "carrier": "payload",
        "service": "HighThroughputPipeline.submit",
        "redacts": False,
        "returns": "PipelineEventAccepted",
        "note": (
            "deliberate, and stated here so it is not read as an oversight: the "
            "pipeline is not the audit trail, and a dead-letter replay needs the "
            "payload intact to be worth replaying"
        ),
    },
    "POST /audit/pipeline/replay": {
        "model": "PipelineReplayRequest",
        "carrier": None,
        "service": "HighThroughputPipeline.replay",
        "redacts": False,
        "returns": "PipelineReplayReport",
        "note": "bounded replay parameters; carries no payload",
    },
}


def contract_redaction_report(routes: Optional[Any] = None) -> ContractRedactionReport:
    """Which carriers cross a trust boundary, and who scrubbed them.

    The load-bearing output is ``write_paths``: for each route that carries a
    free-form blob, whether redaction runs. ``redacts`` is a declared fact about
    the service, and it is the kind of fact that belongs next to the field
    declaration rather than in a service docstring nobody reading the schema will
    open. Where the declaration and the behaviour disagree, the *declaration* is
    what is reported as the defect -- adding ``description="credential keys are
    redacted"`` to one field and not the other is the cheap fix, and even that
    leaves a reader comparing two identical types.
    """

    models = _audit_models()
    observed = _route_map(routes)
    findings: List[Dict[str, Any]] = []

    carriers: List[Dict[str, Any]] = []
    carriers_on_responses: List[Dict[str, Any]] = []
    for model_name, model in models.items():
        row = CONTRACT_INVENTORY.get(model_name) or {}
        kind = str(row.get("kind") or _kind_for(model_name) or "out")
        for field in _field_list(model):
            facts = _field_facts(model, field)
            if not facts["resolved"]:
                continue
            shape = _shape_of(getattr(model.model_fields[field], "annotation", None))
            if not (shape.get("is_dict_str_any") or shape.get("element_is_dict_str_any")):
                continue
            policy = CONTRACT_FIELD_POLICIES.get(field) or {}
            entry = {
                "subject": f"{model_name}.{field}",
                "field": field,
                "direction": "in" if kind == "create" else "out",
                "audience": str(row.get("audience") or "machine_client"),
                "declared_bound": None,
                "description": facts.get("description"),
                "policy_note": str(policy.get("note") or ""),
            }
            carriers.append(entry)
            if kind != "create":
                carriers_on_responses.append(entry)
                findings.append(
                    _finding(
                        "CONTRACT_CARRIER_ON_RESPONSE",
                        "contract_redaction_report",
                        f"{model_name}.{field}",
                        "a free-form carrier is published, so its contents cross the boundary verbatim",
                        {
                            "direction": "out",
                            "audience": entry["audience"],
                            "declared_bound": None,
                        },
                    )
                )

    write_paths: List[Dict[str, Any]] = []
    redacted = 0
    unredacted = 0
    for route, row in sorted(CONTRACT_WRITE_PATHS.items()):
        model_name = str(row.get("model") or "")
        carrier = row.get("carrier")
        describes = _describe_carrier_field(model_name, carrier, models)
        observed_here = (
            sorted((observed["models"].get(model_name) or {}).get("body", []))
            if observed["available"]
            else []
        )
        entry = {
            "route": route,
            "model": model_name,
            "carrier": carrier,
            "service": row.get("service"),
            "redacts": bool(row.get("redacts")),
            "declared_bound": None,
            "returns": row.get("returns"),
            "field_description": describes.get("description"),
            "field_type": describes.get("annotation"),
            "route_observed": route in observed_here,
            "note": str(row.get("note") or ""),
        }
        write_paths.append(entry)
        if carrier is None:
            continue
        if row.get("redacts"):
            redacted += 1
            if not describes.get("description"):
                findings.append(
                    _finding(
                        "CONTRACT_REDACTION_UNDECLARED",
                        "contract_redaction_report",
                        f"{route} ({model_name}.{carrier})",
                        (
                            "the service redacts this carrier and the field declares no "
                            "description saying so; a client reading the OpenAPI document "
                            "cannot learn it from the contract"
                        ),
                        {"service": row.get("service"), "declared_bound": None},
                    )
                )
        else:
            unredacted += 1
            if not str(row.get("note") or ""):
                # An unredacted carrier with no stated reason is the finding.
                # PipelineEventCreate.payload is unredacted too, and its row says
                # why -- so this stays clean and says so.
                findings.append(
                    _finding(
                        "CONTRACT_WRITE_UNREDACTED",
                        "contract_redaction_report",
                        f"{route} ({model_name}.{carrier})",
                        "a carrier write path that stores its payload unscrubbed, with no stated reason",
                        {"service": row.get("service"), "declared_bound": None},
                    )
                )

    # Two write paths, the same declared carrier, opposite behaviour.
    carriers_by_field: Dict[str, List[Dict[str, Any]]] = {}
    for entry in write_paths:
        if entry["carrier"]:
            carriers_by_field.setdefault(str(entry["carrier"]), []).append(entry)
    for carrier, entries in sorted(carriers_by_field.items()):
        flags = {bool(e["redacts"]) for e in entries}
        if len(flags) < 2:
            continue
        signatures = {
            (e["field_type"], e["field_description"]) for e in entries
        }
        if len(signatures) != 1:
            continue
        findings.append(
            _finding(
                "CONTRACT_REDACTION_ASYMMETRY",
                "contract_redaction_report",
                carrier,
                (
                    f"{len(entries)} write paths declare {carrier} identically "
                    f"({entries[0]['field_type']}, no description) and disagree about whether "
                    "to scrub it, so the contract cannot tell a client which is which"
                ),
                {
                    "routes": [e["route"] for e in entries],
                    "redacts": {e["route"]: e["redacts"] for e in entries},
                    "declared_type": entries[0]["field_type"],
                },
            )
        )

    forbidden_present: List[Dict[str, Any]] = []
    for model_name, model in models.items():
        kind = str((CONTRACT_INVENTORY.get(model_name) or {}).get("kind") or _kind_for(model_name) or "")
        if kind == "create":
            continue
        for field in _field_list(model):
            if field not in CONTRACT_FORBIDDEN_FIELDS:
                continue
            forbidden_present.append({"subject": f"{model_name}.{field}", "model": model_name})
            findings.append(
                _finding(
                    "CONTRACT_FORBIDDEN_FIELD_PRESENT",
                    "contract_redaction_report",
                    f"{model_name}.{field}",
                    CONTRACT_FORBIDDEN_FIELDS[field].get("reason", "forbidden name"),
                    {"publish_instead": CONTRACT_FORBIDDEN_FIELDS[field].get("publish_instead")},
                )
            )

    return ContractRedactionReport(
        generated_at=datetime.now(),
        carriers=carriers,
        write_paths=write_paths,
        redacted_paths=redacted,
        unredacted_paths=unredacted,
        forbidden_names=sorted(CONTRACT_FORBIDDEN_FIELDS),
        forbidden_present=forbidden_present,
        carriers_on_responses=carriers_on_responses,
        findings=findings,
        finding_count=len(findings),
        boundary=(
            "a declared field is a published shape and a redacted carrier key is not: "
            "this module checks names, and the keys inside detail/payload are the "
            "writer's responsibility, enforced by redact_detail and reported by the "
            "no_unredacted_credentials gate"
        ),
        note=(
            "the redacts flag is a declared fact about the service, asserted against "
            "it in the tests; the schema alone cannot know which paths scrub a carrier"
        ),
    )


def _describe_carrier_field(
    model_name: str, field: Optional[str], models: Dict[str, type]
) -> Dict[str, Any]:
    """What the schema says about one carrier field, for the redaction table."""

    if not field or model_name not in models:
        return {}
    facts = _field_facts(models[model_name], field)
    return {
        "description": facts.get("description"),
        "annotation": facts.get("annotation"),
        "max_length": facts.get("max_length"),
    }


def validate_contracts() -> ContractValidationReport:
    """Is this governance layer itself configured coherently.

    Every check here is about the *tables*, not the contracts, and none of them
    raises. A validator that dies on a malformed row cannot report the row; it
    can only take the endpoint down. Each failure is reported with the table and
    the key, and the whole thing returns a report either way.
    """

    messages: List[str] = []
    coded: Dict[str, List[str]] = {}
    # Keyed by the taxonomy's own severity vocabulary, not by "error". Four
    # rows used to carry severity="error" while twenty-nine used defect /
    # warning / info, so one payload had two vocabularies for one field -- the
    # same collision this module reports for `severity` on the contracts. A
    # `defect` is what makes the layer not-ok; `errors` is that count, kept
    # under its pinned name.
    counts = {"defect": 0, "warning": 0, "info": 0}
    models = _audit_models()

    def note(code: str, subject: str, message: str) -> None:
        row = CONTRACT_WARNINGS.get(code) or {}
        severity = str(row.get("severity", "defect"))
        if severity not in counts:
            severity = "defect"
        counts[severity] = counts.get(severity, 0) + 1
        coded.setdefault(code, []).append(subject)
        messages.append(f"[{severity}] {code} {subject}: {message}")

    # 1. every op is report_only -- the posture is a checked property
    for op, row in sorted(CONTRACT_OPS.items()):
        if not isinstance(row, dict):
            note("CONTRACT_TABLE_MALFORMED", f"CONTRACT_OPS.{op}", "the row is not a mapping")
            continue
        if row.get("report_only") is not True:
            note(
                "CONTRACT_OP_NOT_REPORT_ONLY",
                op,
                "report_only is not True, so the layer claims a power it does not have",
            )

    # 2. every audience and kind the inventory names exists. Three codes, not
    #    one: a bad kind reported as an unknown audience is the same category of
    #    mistake as a vocabulary typo filed under the wrong name -- the code
    #    says what is wrong is not what is wrong.
    for model_name, row in sorted(CONTRACT_INVENTORY.items()):
        if not isinstance(row, dict):
            note("CONTRACT_TABLE_MALFORMED", f"CONTRACT_INVENTORY.{model_name}", "the row is not a mapping")
            continue
        if row.get("audience") not in CONTRACT_AUDIENCES:
            note(
                "CONTRACT_AUDIENCE_UNKNOWN",
                f"{model_name}.audience",
                f"{row.get('audience')!r} is not in CONTRACT_AUDIENCES",
            )
        if row.get("kind") not in CONTRACT_KINDS:
            note(
                "CONTRACT_KIND_UNKNOWN",
                f"{model_name}.kind",
                f"{row.get('kind')!r} is not in CONTRACT_KINDS",
            )
        if row.get("kind") == "create" and row.get("top_level") is True:
            note(
                "CONTRACT_KIND_UNKNOWN",
                f"{model_name}.top_level",
                "a request-body contract declared top_level, which contradicts CONTRACT_KINDS",
            )

    # 3. field policies: used, resolving, and honest about their carrier
    used_policies: set = set()
    embedded_accepted = 0
    shapes: Dict[str, Dict[str, List[str]]] = {}
    for model_name, model in models.items():
        for field in _field_list(model):
            policy = CONTRACT_FIELD_POLICIES.get(field)
            facts = _field_facts(model, field)
            shape = _shape_of(getattr(model.model_fields[field], "annotation", None))
            shape_key = _shape_key(shape)
            shapes.setdefault(field, {}).setdefault(shape_key, []).append(
                f"{model_name}.{field}"
            )
            if not policy:
                continue
            used_policies.add(field)
            declared_carrier = str(policy.get("carrier") or "")
            actual = _actual_carrier(shape, field)
            if declared_carrier == "embedded" and actual == "scalar":
                # A str that carries a document is exactly what `embedded` is
                # for, and an annotation cannot tell one from a label. Counting
                # it as accepted rather than as a mismatch is the difference
                # between a check that reports clean and one that has learned
                # to ignore its own vocabulary.
                embedded_accepted += 1
                continue
            if declared_carrier and actual and declared_carrier != actual:
                note(
                    "CONTRACT_POLICY_CARRIER_MISMATCH",
                    f"{model_name}.{field}",
                    f"the policy says carrier={declared_carrier!r} and the annotation is {actual!r} "
                    f"({facts.get('annotation')})",
                )
    for field in sorted(set(CONTRACT_FIELD_POLICIES) - used_policies):
        note(
            "CONTRACT_POLICY_UNUSED",
            field,
            "no field in the module carries this name, so the row documents something that no longer exists",
        )

    # 3a. one name, more than one shape. The container-vs-scalar cases are
    #     benign; the ones worth reporting are where the *meaning* changes, and
    #     those are the ones where two shapes disagree on a primitive: a bool
    #     against an int is a flag against a count, not a list against a dict.
    for field, variants in sorted(shapes.items()):
        if len(variants) < 2:
            continue
        primitives = {
            key
            for key in variants
            if not (key.startswith("list:") or key.startswith("dict:") or key == "any")
        }
        if len(primitives) < 2:
            continue
        note(
            "CONTRACT_FIELD_SHAPE_COLLISION",
            field,
            f"{len(variants)} shapes under one name, disagreeing on a primitive type: "
            + "; ".join(f"{k} -> {v[0]}" for k, v in sorted(variants.items())),
        )

    # 4. field names with no policy. Reported once per name, not per field, and
    #    never for a name the trivial table covers.
    recurring: Dict[str, int] = {}
    for model in models.values():
        for field in _field_list(model):
            recurring[field] = recurring.get(field, 0) + 1
    for field, count in sorted(recurring.items()):
        if field in CONTRACT_FIELD_POLICIES or field in CONTRACT_TRIVIAL_FIELDS or count < 3:
            continue
        note(
            "CONTRACT_FIELD_UNDECLARED",
            field,
            f"{count} fields carry this name and no policy row says who may read them",
        )

    # 5. the forbidden-name table must not name a field the module actually uses
    for name in sorted(set(CONTRACT_FORBIDDEN_FIELDS) & set(recurring)):
        note(
            "CONTRACT_FORBIDDEN_FIELD_PRESENT",
            name,
            "the table forbids a name the module declares on a contract",
        )

    # 6. every write path names a model that exists and a carrier that model has
    for route, row in sorted(CONTRACT_WRITE_PATHS.items()):
        if not isinstance(row, dict):
            note("CONTRACT_TABLE_MALFORMED", f"CONTRACT_WRITE_PATHS.{route}", "the row is not a mapping")
            continue
        model_name = str(row.get("model") or "")
        if model_name not in models:
            note("CONTRACT_WRITE_PATH_UNKNOWN", route, f"{model_name!r} is not a contract in this module")
            continue
        carrier = row.get("carrier")
        if carrier and carrier not in _field_list(models[model_name]):
            note(
                "CONTRACT_POLICY_CARRIER_MISMATCH",
                route,
                f"{model_name} has no field {carrier!r}",
            )

    # 7. the taxonomy itself. This check exists because it caught its own table:
    #    one row had emitted_by as a bare string, so `",".join(row["emitted_by"])`
    #    spelled the producer name out one character at a time and printed clean.
    #    A check that reads the taxonomy as a consumer of it is the only thing
    #    that can notice -- the report the row describes was never wrong.
    producers = {
        "contract_inventory",
        "describe_contract_field",
        "contract_divergence_report",
        "contract_redaction_report",
        "validate_contracts",
        "build_contract_catalog",
    }
    for code, row in sorted(CONTRACT_WARNINGS.items()):
        where = f"CONTRACT_WARNINGS.{code}"
        if not isinstance(row, dict):
            note("CONTRACT_TAXONOMY_MALFORMED", where, "the row is not a mapping")
            continue
        if row.get("severity") not in {"defect", "warning", "info"}:
            note(
                "CONTRACT_TAXONOMY_MALFORMED",
                where,
                f"severity={row.get('severity')!r} is not one of defect/warning/info, "
                "so a finding built from this row would be counted under an unknown key",
            )
        if not str(row.get("description") or "").strip():
            note("CONTRACT_TAXONOMY_MALFORMED", where, "description is empty")
        emitted_by = row.get("emitted_by")
        if isinstance(emitted_by, str) or not isinstance(emitted_by, (list, tuple)):
            note(
                "CONTRACT_TAXONOMY_MALFORMED",
                where,
                f"emitted_by={emitted_by!r} is not a sequence; a bare string is iterated "
                "character by character by every consumer of this field",
            )
        else:
            unknown = [name for name in emitted_by if name not in producers]
            if unknown:
                note(
                    "CONTRACT_TAXONOMY_MALFORMED",
                    where,
                    f"emitted_by names {unknown!r}, which is not a producer in this module",
                )

    errors = counts.get("defect", 0)
    return ContractValidationReport(
        generated_at=datetime.now(),
        ok=errors == 0,
        errors=errors,
        warnings=counts.get("warning", 0),
        info=counts.get("info", 0),
        counts=dict(counts),
        coded={code: sorted(subjects) for code, subjects in sorted(coded.items())},
        checks={
            "ops_are_report_only": len(CONTRACT_OPS),
            "audiences_referenced": len(CONTRACT_INVENTORY),
            "field_policies": len(CONTRACT_FIELD_POLICIES),
            "field_policies_used": len(used_policies),
            "embedded_carriers_accepted": embedded_accepted,
            "trivial_names": len(CONTRACT_TRIVIAL_FIELDS),
            "forbidden_names": len(CONTRACT_FORBIDDEN_FIELDS),
            "write_paths": len(CONTRACT_WRITE_PATHS),
            "models": len(models),
            "recurring_names": sum(1 for c in recurring.values() if c >= 3),
        },
        messages=messages,
        note=(
            "this validates the tables, not the contracts: a clean report means the "
            "governance layer is coherent, never that the contracts it describes are"
        ),
    )


def build_contract_catalog(routes: Optional[Any] = None) -> ContractCatalogReport:
    """The whole governance layer in one payload.

    Each report also stands alone -- this is a roll-up, not the only place a
    finding appears. ``unreached_codes`` is the part worth arguing about: a
    taxonomy row whose code no report emitted is a guard that only fires on a
    malformed table, and saying so is more useful than deleting the row or
    pretending it is covered.
    """

    inventory = contract_inventory(routes=routes)
    divergence = contract_divergence_report(routes=routes)
    redaction = contract_redaction_report(routes=routes)
    validation = validate_contracts()

    findings: List[ContractFindingOut] = []
    for report in (inventory, divergence, redaction):
        findings.extend(
            item if isinstance(item, ContractFindingOut) else ContractFindingOut(**item)
            for item in report.findings
        )
    for code, subjects in validation.coded.items():
        row = CONTRACT_WARNINGS.get(code) or {}
        for subject in subjects:
            findings.append(
                ContractFindingOut(
                    code=code,
                    severity=str(row.get("severity", "error")),
                    producer="validate_contracts",
                    subject=subject,
                    detail=next(
                        (
                            message.split(": ", 1)[-1]
                            for message in validation.messages
                            if code in message and subject in message
                        ),
                        str(row.get("description", "")),
                    ),
                    evidence={},
                )
            )

    counts: Dict[str, int] = {}
    for item in findings:
        counts[item.severity] = counts.get(item.severity, 0) + 1

    emitted = {item.code for item in findings}
    # The taxonomy code is excluded from its own unreached set: it is about to
    # be emitted, and a row that reports "no report emitted this" while doing
    # exactly that is the same shape of claim as the redaction table saying a
    # path redacts because the word appears somewhere in the file.
    unreached = sorted(
        code
        for code in CONTRACT_WARNINGS
        if code not in emitted and code != "CONTRACT_TAXONOMY_NOT_EMITTED"
    )
    # Emit the unreached set rather than only counting it. A field named
    # `unreached_codes` that no report ever surfaces is a claim a client has to
    # take on trust; as findings they are rows in the same list as everything
    # else, and the catalog's own count is checkable against them.
    for code in unreached:
        row = CONTRACT_WARNINGS.get(code) or {}
        findings.append(
            ContractFindingOut(
                code="CONTRACT_TAXONOMY_NOT_EMITTED",
                severity=str(row.get("severity", "info")),
                producer="build_contract_catalog",
                subject=code,
                detail=(
                    f"no report emitted {code!r}: {row.get('description', '')} "
                    "It is a guard that only fires on a malformed table."
                ),
                evidence={"declared_severity": row.get("severity", "info")},
            )
        )
    counts = {}
    for item in findings:
        counts[item.severity] = counts.get(item.severity, 0) + 1
    clean = [
        name
        for name, report in (
            ("contract_inventory", inventory),
            ("contract_divergence_report", divergence),
            ("contract_redaction_report", redaction),
            ("validate_contracts", validation),
        )
        if not (report.finding_count if hasattr(report, "finding_count") else report.errors or report.warnings or report.info)
    ]

    return ContractCatalogReport(
        generated_at=datetime.now(),
        version=CONTRACT_GOVERNANCE_VERSION,
        policy={
            "posture": "report, never repair",
            "why": (
                "a pydantic model here is not a rendering of a policy, it is the "
                "contract. Widening severity to silence a finding would delete the "
                "only place the vocabulary was written down and start accepting a "
                "fourth value at the door"
            ),
            "enforced_by_check": "validate_contracts errors on any CONTRACT_OPS row that is not report_only",
            "not_imported": (
                "app.models and app.main are deliberately not imported: the DDL and "
                "the route table are facts this module does not own, so the database "
                "constraint is documented and not verified, and the route checks take "
                "the openapi document as an argument and report themselves as skipped "
                "without it"
            ),
            "shipped_unchanged": (
                "all 40 models above keep their exact names, fields, annotations, "
                "defaults, constraints and requiredness; no route's payload changes shape"
            ),
        },
        kinds={name: dict(row) for name, row in CONTRACT_KINDS.items()},
        audiences={name: dict(row) for name, row in CONTRACT_AUDIENCES.items()},
        field_policies={name: dict(row) for name, row in CONTRACT_FIELD_POLICIES.items()},
        forbidden_fields={name: dict(row) for name, row in CONTRACT_FORBIDDEN_FIELDS.items()},
        ops={name: dict(row) for name, row in CONTRACT_OPS.items()},
        findings_taxonomy={name: dict(row) for name, row in CONTRACT_WARNINGS.items()},
        inventory=inventory,
        divergence=divergence,
        redaction=redaction,
        validation=validation,
        findings=findings,
        finding_count=len(findings),
        severity_counts=dict(sorted(counts.items())),
        unreached_codes=unreached,
        clean_reports=sorted(clean),
        ground_truth={
            # `(row or {})`: this module's own rule is that a malformed table is
            # reported, not raised on -- and a row set to None made the catalog
            # die on its own ground-truth arithmetic, the one place a client
            # reads to check the report against.
            "models": len(_audit_models()),
            "request_bodies": sum(
                1
                for row in CONTRACT_INVENTORY.values()
                if (row or {}).get("kind") == "create"
            ),
            "top_level_responses": sum(
                1 for row in CONTRACT_INVENTORY.values() if (row or {}).get("top_level")
            ),
            "nested_only": sum(
                1
                for row in CONTRACT_INVENTORY.values()
                if (row or {}).get("top_level") is False
                and (row or {}).get("kind") != "create"
            ),
            "fields_examined": divergence.fields_examined,
            "codes": len(CONTRACT_WARNINGS),
            # counted after the unreached rows were appended, so that
            # emitted + unreached == codes holds and a client can check the
            # arithmetic instead of trusting three numbers separately
            "emitted_codes": len({item.code for item in findings}),
            "route_observed": inventory.route_observed,
        },
        note=(
            "40 contracts, one module, and the facts a schema file cannot state: which "
            "write path scrubs a carrier, which route has no response contract, and which "
            "name means two different things"
        ),
    )
