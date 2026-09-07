from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

class ChatMessageIn(BaseModel):
    message: str


class InteractionInsight(BaseModel):
    area: str
    priority: str
    score: float
    evidence: List[str]
    evidence_summary: str
    source: str
    recommendation: str
    next_step: str


class InteractionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    messages_analyzed: int
    bookings_analyzed: int
    churn_risk: str
    loyalty_score: float
    monetization_readiness: float
    value_tier: str
    customer_classification: str
    top_issues: List[str]
    strengths: List[str]
    insights: List[InteractionInsight]
    metadata: Dict[str, Any]
    generated_at: datetime


class SystemImprovementItem(BaseModel):
    area: str
    priority: str
    impact: str
    rationale: List[str]
    recommendation: str
    owner_hint: str


class SystemImprovementPack(BaseModel):
    user_id: int
    generated_at: datetime
    focus: str
    items: List[SystemImprovementItem]
    summary: InteractionSummary


class WeightedFocusItem(BaseModel):
    area: str
    weight: float
    importance: str
    rationale: List[str]
    focus: str


class WeightedSystemMonitoringReport(BaseModel):
    user_id: int
    generated_at: datetime
    scope: str
    items: List[WeightedFocusItem]
    summary: InteractionSummary


class InteractionTrendItem(BaseModel):
    area: str
    current_score: float
    previous_score: float
    delta: float
    direction: str


class InteractionTrendReport(BaseModel):
    user_id: int
    window_days: int
    trends: List[InteractionTrendItem]
    improving_areas: List[str]
    worsening_areas: List[str]
    generated_at: datetime


class RetentionCohortItem(BaseModel):
    cohort: str
    user_count: int
    avg_loyalty_score: float
    avg_signal_score: float
    avg_monetization_readiness: float
    primary_risk: str
    recommended_action: str
    cohort_rule: str


class MonetizationCohortItem(BaseModel):
    cohort: str
    user_count: int
    avg_loyalty_score: float
    avg_monetization_readiness: float
    avg_signal_score: float
    primary_risk: str
    recommended_action: str
    cohort_rule: str


class MonetizationCohortReport(BaseModel):
    generated_at: datetime
    window_days: int
    cohorts: List[MonetizationCohortItem]


class RetentionCohortReport(BaseModel):
    generated_at: datetime
    window_days: int
    cohorts: List[RetentionCohortItem]


class RetentionCohortMember(BaseModel):
    user_id: int
    username: str
    cohort: str
    loyalty_score: float
    signal_score: float
    monetization_readiness: float
    primary_risk: str
    strongest_risk_area: str
    recent_booking_state: str


class RetentionCohortDrilldownReport(BaseModel):
    generated_at: datetime
    window_days: int
    cohort: str
    members: List[RetentionCohortMember]


class ChurnPrediction(BaseModel):
    user_id: int
    risk_score: float
    risk_level: str
    warning_reasons: List[str]
    next_best_actions: List[str]
    generated_at: datetime


class LifecycleStageItem(BaseModel):
    user_id: int
    stage: str
    confidence: float
    drivers: List[str]
    retention_focus: List[str]
    churn_prediction: ChurnPrediction


class LifecycleStageReport(BaseModel):
    generated_at: datetime
    window_days: int
    stages: List[LifecycleStageItem]


class RetentionSnapshotItem(BaseModel):
    id: int
    user_id: int
    snapshot_type: str
    window_days: int
    loyalty_score: float
    churn_risk: str
    lifecycle_stage: str
    summary_json: str
    created_at: datetime


class RetentionSnapshotReport(BaseModel):
    generated_at: datetime
    window_days: int
    snapshots: List[RetentionSnapshotItem]


class RetentionSnapshotDelta(BaseModel):
    user_id: int
    window_days: int
    previous_snapshot_id: Optional[int]
    current_snapshot_id: Optional[int]
    loyalty_score_delta: float
    churn_risk_delta: str
    lifecycle_stage_delta: str
    churn_risk_changed: bool
    lifecycle_stage_changed: bool
    previous_created_at: Optional[datetime]
    current_created_at: Optional[datetime]
    generated_at: datetime


class RetentionSnapshotTrendItem(BaseModel):
    snapshot_type: str
    count: int
    avg_loyalty_score: float
    avg_churn_risk_score: float
    latest_created_at: Optional[datetime]


class RetentionSnapshotOperationItem(BaseModel):
    label: str
    status: str
    count: int
    details: List[str]
    recommended_owner: str


class RetentionSnapshotOperationsReport(BaseModel):
    generated_at: datetime
    window_days: int
    total_snapshots: int
    stale_snapshots: int
    recent_snapshots: int
    stale_ratio: float
    items: List[RetentionSnapshotOperationItem]


class RetentionSnapshotTrendReport(BaseModel):
    generated_at: datetime
    window_days: int
    trends: List[RetentionSnapshotTrendItem]


class RetentionDashboard(BaseModel):
    generated_at: datetime
    window_days: int
    summary: InteractionSummary
    churn_prediction: ChurnPrediction
    snapshot_report: RetentionSnapshotReport
    snapshot_delta: RetentionSnapshotDelta
    snapshot_trends: RetentionSnapshotTrendReport
    snapshot_operations_report: Optional[RetentionSnapshotOperationsReport] = None
    trend_coverage: Optional[float] = None
    delta_coverage: Optional[float] = None


class RetentionMaintenanceResult(BaseModel):
    user_id: int
    window_days: int
    removed_snapshots: int
    kept_snapshots: int
    generated_at: datetime


class RetentionMaintenanceReport(BaseModel):
    generated_at: datetime
    window_days: int
    keep: int
    total_users: int
    results: List[RetentionMaintenanceResult]


class EcosystemSubserviceStatus(BaseModel):
    name: str
    purpose: str
    routes: List[str]
    status: str
    health_endpoint: Optional[str] = None
    notes: List[str] = Field(default_factory=list)


class EcosystemStatusReport(BaseModel):
    name: str
    version: str
    environment: str
    generated_at: datetime
    overall_status: str
    subservices: Dict[str, EcosystemSubserviceStatus]


class AuthenticatedProbeTarget(BaseModel):
    route: str
    method: str
    requires_auth: bool
    expected_status: int


class AuthenticatedProbeReport(BaseModel):
    generated_at: datetime
    scope: str
    actor: str
    targets: List[AuthenticatedProbeTarget]
    status: str
    notes: List[str]


class CapabilitySummaryItem(BaseModel):
    key: str
    study_theme: str
    feature: str
    signal_focus: List[str]
    route: str


class CapabilitySummaryReport(BaseModel):
    generated_at: datetime
    title: str
    items: List[CapabilitySummaryItem]


class UserActivityReport(BaseModel):
    user_id: int
    window_days: int
    chat_messages: int
    bookings: int
    snapshots: int
    latest_chat_at: Optional[datetime] = None
    latest_booking_at: Optional[datetime] = None
    latest_snapshot_at: Optional[datetime] = None
    generated_at: datetime


class AdminActivityReport(BaseModel):
    generated_at: datetime
    window_days: int
    total_users: int
    total_chat_messages: int
    total_bookings: int
    total_snapshots: int
    recent_chats: int
    recent_bookings: int
    recent_snapshots: int


class ActivityTimelineItem(BaseModel):
    kind: str
    created_at: datetime
    summary: str
    reference_id: int


class ActivityTimelineReport(BaseModel):
    user_id: int
    window_days: int
    generated_at: datetime
    items: list[ActivityTimelineItem]


class RankedUserItem(BaseModel):
    user_id: int
    username: str
    chat_messages: int
    bookings: int
    snapshots: int
    activity_score: int


class RankedUserReport(BaseModel):
    generated_at: datetime
    window_days: int
    limit: int
    users: list[RankedUserItem]


class AdminRetentionTrendItem(BaseModel):
    area: str
    current_window: int
    previous_window: int
    delta: int


class AdminRetentionTrendReport(BaseModel):
    generated_at: datetime
    window_days: int
    trends: list[AdminRetentionTrendItem]


class RetentionSnapshotAdminItem(BaseModel):
    snapshot_type: str
    count: int
    avg_loyalty_score: float
    latest_created_at: Optional[datetime]


class RetentionSnapshotAdminReport(BaseModel):
    generated_at: datetime
    window_days: int
    total_snapshots: int
    items: list[RetentionSnapshotAdminItem]


class UserRetentionSnapshotHealth(BaseModel):
    user_id: int
    window_days: int
    snapshot_count: int
    latest_snapshot_type: Optional[str] = None
    latest_lifecycle_stage: Optional[str] = None
    avg_loyalty_score: float
    generated_at: datetime


class RetentionSnapshotComparisonItem(BaseModel):
    snapshot_type: str
    current_window: int
    previous_window: int
    delta: int


class RetentionSnapshotComparisonReport(BaseModel):
    generated_at: datetime
    window_days: int
    comparisons: list[RetentionSnapshotComparisonItem]


class RetentionSnapshotMomentumItem(BaseModel):
    snapshot_type: str
    direction: str
    momentum: int


class RetentionSnapshotMomentumReport(BaseModel):
    generated_at: datetime
    window_days: int
    items: list[RetentionSnapshotMomentumItem]


class RetentionSnapshotVolatilityItem(BaseModel):
    snapshot_type: str
    volatility: int
    classification: str


class RetentionSnapshotVolatilityReport(BaseModel):
    generated_at: datetime
    window_days: int
    items: list[RetentionSnapshotVolatilityItem]


class RetentionSnapshotVolatilitySummary(BaseModel):
    generated_at: datetime
    window_days: int
    stable: int
    moderate: int
    high: int


class RetentionSnapshotRiskProfile(BaseModel):
    generated_at: datetime
    window_days: int
    risk_level: str
    score: int


class RetentionSnapshotRecommendation(BaseModel):
    generated_at: datetime
    window_days: int
    risk_level: str
    recommendation: str


class RetentionSnapshotActionPlan(BaseModel):
    generated_at: datetime
    window_days: int
    risk_level: str
    action: str


class RecoverySignal(BaseModel):
    area: str
    intensity: float
    evidence: List[str]
    evidence_summary: str
    recommended_action: str


class DissatisfactionRecoveryReport(BaseModel):
    generated_at: datetime
    window_days: int
    dissatisfaction_score: float
    recovery_readiness: str
    primary_risks: List[str]
    recovery_signals: list[RecoverySignal]
    action_plan: str


class RecoverySnapshotImpact(BaseModel):
    area: str
    score: float
    evidence: List[str]
    action: str


class RecoverySnapshotSummary(BaseModel):
    generated_at: datetime
    window_days: int
    recovery_readiness: str
    primary_risks: List[str]
    impacts: List[RecoverySnapshotImpact]
    action_plan: str


class RetentionSnapshotAuditItem(BaseModel):
    snapshot_type: str
    total_snapshots: int
    unique_users: int
    latest_created_at: Optional[datetime]


class RetentionSnapshotAuditReport(BaseModel):
    generated_at: datetime
    window_days: int
    total_snapshots: int
    items: list[RetentionSnapshotAuditItem]


class RetentionSnapshotAuditExport(BaseModel):
    generated_at: datetime
    window_days: int
    summary: str
    snapshot_types: list[str]
    total_snapshots: int


class RetentionSnapshotTypeBreakdownItem(BaseModel):
    snapshot_type: str
    total_snapshots: int
    unique_users: int


class RetentionSnapshotTypeBreakdownReport(BaseModel):
    generated_at: datetime
    window_days: int
    snapshot_type: str
    total_snapshots: int
    unique_users: int
    items: list[RetentionSnapshotTypeBreakdownItem]


class RetentionSnapshotStalenessItem(BaseModel):
    snapshot_type: str
    stale_snapshots: int
    fresh_snapshots: int
    staleness_rate: float
    latest_created_at: Optional[datetime]


class RetentionSnapshotStalenessReport(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    total_stale_snapshots: int
    items: list[RetentionSnapshotStalenessItem]


class RetentionSnapshotStalenessTrendItem(BaseModel):
    bucket: str
    stale_snapshots: int
    fresh_snapshots: int
    staleness_rate: float


class RetentionSnapshotStalenessTrendReport(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    items: list[RetentionSnapshotStalenessTrendItem]


class RetentionSnapshotHealthScore(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    score: int
    status: str


class RetentionSnapshotHealthSummary(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    score: int
    status: str
    summary: str


class RetentionSnapshotHealthRisk(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    score: int
    status: str
    risk_level: str


class RetentionSnapshotHealthRecommendation(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    score: int
    status: str
    risk_level: str
    recommendation: str


class RetentionSnapshotOperationsOverview(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    score: int
    status: str
    risk_level: str
    recommendation: str
    overview: str


class RetentionSnapshotOperationsStatus(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    status: str
    overview: str


class RetentionSnapshotOperationsCompliance(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    compliance: str
    overview: str


class RetentionSnapshotOperationsPosture(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    posture: str
    overview: str


class RetentionSnapshotOperationsAutomation(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    automation_ready: str
    overview: str


class RetentionSnapshotOperationsExecutionState(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    execution_state: str
    overview: str


class RetentionSnapshotOperationsLaunchReadiness(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    launch_readiness: str
    overview: str


class RetentionSnapshotOperationsGoNoGo(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    decision: str
    overview: str


class LoyaltyRecoveryReport(BaseModel):
    generated_at: datetime
    window_days: int
    loyalty_score: float
    churn_risk: str
    recovery_readiness: str
    dissatisfaction: DissatisfactionRecoveryReport
    retention_recommendation: RetentionSnapshotRecommendation
    action_plan: RetentionSnapshotActionPlan


class Section11ExpansionSummary(BaseModel):
    generated_at: datetime
    window_days: int
    dissatisfaction_recovery: DissatisfactionRecoveryReport
    loyalty_recovery: LoyaltyRecoveryReport
    recovery_snapshot: RecoverySnapshotSummary

class Sentiment(BaseModel):
    label: str
    score: float

class ChatMessageOut(BaseModel):
    text: str
    sentiment: Optional[Sentiment]
    suggestions: Optional[List[str]]
    vector: Optional[List[float]]
    insights: Optional[List[InteractionInsight]] = None
    summary: Optional[InteractionSummary] = None
    improvement_pack: Optional[SystemImprovementPack] = None

class ChatHistoryCreate(BaseModel):
    user_id: int
    message: str
    response: str

class ChatHistoryOut(BaseModel):
    id: int
    user_id: Optional[int] = None
    message: str
    response: str
    timestamp: str


class ChatHistorySummary(BaseModel):
    user_id: Optional[int] = None
    total_messages: int
    total_responses: int