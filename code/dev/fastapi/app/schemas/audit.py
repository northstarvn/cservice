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
