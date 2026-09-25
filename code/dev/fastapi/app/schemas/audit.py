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