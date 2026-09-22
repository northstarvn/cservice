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


class TopicRankingItem(BaseModel):
    topic: str
    score: float
    priority: str
    impact: str
    owner_hint: str
    evidence: List[str]
    evidence_summary: str
    recommendation: str
    next_step: str


class TopicRankingReport(BaseModel):
    generated_at: datetime
    window_days: int
    topics: List[TopicRankingItem]


class TopicPolicyDecision(BaseModel):
    topic: str
    outcome: str
    rationale: List[str]
    rule_version: str
    recommended_action: str


class TopicPolicyDecisionReport(BaseModel):
    generated_at: datetime
    window_days: int
    decisions: List[TopicPolicyDecision]


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


# ---------------------------------------------------------------------------
# Signal synthesis / retention topic coverage
# ---------------------------------------------------------------------------

class TopicThemeCoverageItem(BaseModel):
    theme: str
    topic_count: int
    matched_count: int
    coverage: float
    matched_topics: List[str] = Field(default_factory=list)
    related_topics: List[str] = Field(default_factory=list)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


class SignalSynthesisReport(BaseModel):
    generated_at: datetime
    user_id: int
    summary: InteractionSummary
    topic_breakdown: "TopicSignalBreakdown"
    sentiment_bridge: "SentimentRetentionBridge"
    timeline: List["SignalSynthesisTimelineItem"]
    recommended_focus: str
    dominant_topic: Optional[str] = None
    retention_risk: str
    topic_context: str = ""
    topic_theme_coverage: List[TopicThemeCoverageItem] = Field(default_factory=list)
    topic_focus: List[str] = Field(default_factory=list)


class TopicSignalBreakdown(BaseModel):
    generated_at: datetime
    user_id: int
    topic_counts: Dict[str, int]
    top_topics: List[Dict[str, int | str]]
    dominant_topic: Optional[str] = None
    topic_diversity: float


class SignalSynthesisTimelineItem(BaseModel):
    label: str
    value: str
    severity: str


class SentimentRetentionBridge(BaseModel):
    generated_at: datetime
    user_id: int
    dissatisfaction_score: float
    recovery_readiness: str
    retention_risk: str
    primary_risks: List[str]
    action_plan: str
    topic_signal_count: int = 0
    topic_signal_depth: str = ""
    topic_context: str = ""
    topic_theme_coverage: List[TopicThemeCoverageItem] = Field(default_factory=list)


class InteractionSignalSynthesis(BaseModel):
    generated_at: datetime
    user_id: int
    topic_breakdown: TopicSignalBreakdown
    sentiment_bridge: SentimentRetentionBridge
    loyalty_score: float
    monetization_readiness: float
    churn_risk: str
    value_tier: str
    customer_classification: str
    signal_strength: float
    topic_context: str = ""
    topic_theme_coverage: List[TopicThemeCoverageItem] = Field(default_factory=list)
    topic_focus: List[str] = Field(default_factory=list)


class SignalSynthesisBundle(BaseModel):
    generated_at: datetime
    summary: InteractionSummary
    signal_synthesis: InteractionSignalSynthesis
    topic_breakdown: TopicSignalBreakdown
    sentiment_bridge: SentimentRetentionBridge
    topic_focus: List[str] = Field(default_factory=list)
    topic_theme_coverage: List[TopicThemeCoverageItem] = Field(default_factory=list)
    retention_risk: str
    summary_text: str = ""


class DissatisfactionTimelineItem(BaseModel):
    label: str
    value: str
    severity: str


class DissatisfactionTimeline(BaseModel):
    generated_at: datetime
    user_id: int
    items: List[DissatisfactionTimelineItem]


class RetentionTopicSignalItem(BaseModel):
    topic: str
    matched_keywords: List[str] = Field(default_factory=list)
    matched_themes: List[str] = Field(default_factory=list)
    matched_sectors: List[str] = Field(default_factory=list)
    coverage_score: float = 0.0


class RetentionTopicSignalReport(BaseModel):
    generated_at: datetime
    user_id: int
    window_days: int
    topic_context: str
    dominant_topic: Optional[str] = None
    matched_topics: List[str] = Field(default_factory=list)
    matched_keywords: List[str] = Field(default_factory=list)
    matched_themes: List[str] = Field(default_factory=list)
    matched_topic_details: List[Dict[str, Any]] = Field(default_factory=list)
    matched_clusters: Dict[str, List[str]] = Field(default_factory=dict)
    topic_catalog_size: int = 0
    topic_portfolio_coverage: float = 0.0
    topic_suggestions: List[str] = Field(default_factory=list)
    topic_theme_overlap_score: int = 0
    topic_focus: List[str] = Field(default_factory=list)
    topic_signal_depth: str = ""
    topic_signal_richness: float = 0.0
    items: List[RetentionTopicSignalItem] = Field(default_factory=list)
    summary: str = ""


class RetentionTopicSignalDetail(BaseModel):
    topic: str
    topic_context: str
    dominant_topic: Optional[str] = None
    items: List[RetentionTopicSignalItem] = Field(default_factory=list)
    topic_theme_coverage: List[TopicThemeCoverageItem] = Field(default_factory=list)
    matched_clusters: Dict[str, List[str]] = Field(default_factory=dict)
    matched_topics: List[str] = Field(default_factory=list)
    matched_keywords: List[str] = Field(default_factory=list)
    matched_themes: List[str] = Field(default_factory=list)
    topic_portfolio_coverage: float = 0.0
    topic_signal_count: int = 0
    topic_signal_depth: str = ""
    topic_signal_summary: str = ""
    summary: str = ""


class RetentionCoverageItem(BaseModel):
    label: str
    count: int
    ratio: float


class RetentionCoverageReport(BaseModel):
    generated_at: datetime
    user_id: int
    window_days: int
    total_snapshots: int
    items: List[RetentionCoverageItem]
    topic_coverage_ratio: float = 0.0
    summary: str = ""


class RetentionOperationalItem(BaseModel):
    name: str
    status: str
    detail: str
    owner_hint: str


class RetentionOperationalReport(BaseModel):
    generated_at: datetime
    user_id: int
    window_days: int
    items: List[RetentionOperationalItem]
    summary: str


# ---------------------------------------------------------------------------
# Policy topic analysis + topic coverage
# ---------------------------------------------------------------------------

class PolicyScoreBreakdown(BaseModel):
    system_score: float
    customer_score: float
    access_score: float
    interest_score: float
    closeness_score: float
    community_closeness_score: float
    policy_tier: str
    control_posture: str
    access_band: str
    summary: str


class PolicyScoreReport(BaseModel):
    generated_at: datetime
    snapshot: PolicyScoreBreakdown
    summary: str


class PolicyTopicInsightItem(BaseModel):
    topic: str
    breadth: float
    complexity: float
    richness: str
    depth: str
    fallback: str
    theme_coverage: List[Dict[str, Any]] = Field(default_factory=list)
    sector_coverage: List[str] = Field(default_factory=list)
    matched_keywords: List[str] = Field(default_factory=list)
    confidence: float = 0.0


class PolicyTopicAnalysisReport(BaseModel):
    generated_at: datetime
    user_id: int
    current_topic: str
    topic_context: str
    topic_richness: str
    topic_depth: str
    topic_coverage: List[Dict[str, Any]] = Field(default_factory=list)
    portfolio_coverage: float = 0.0
    matched_themes: List[str] = Field(default_factory=list)
    topic_signal_count: int = 0
    items: List[PolicyTopicInsightItem]
    summary: str


class TopicCoverageThemeItem(BaseModel):
    theme: str
    coverage: float
    topic_count: int


class TopicCoverageReport(BaseModel):
    generated_at: datetime
    topic: Optional[str] = None
    matched_topics: List[str] = Field(default_factory=list)
    matched_topic_count: int = 0
    keyword_matches: List[str] = Field(default_factory=list)
    keyword_match_count: int = 0
    coverage_ratio: float = 0.0
    catalog_size: int = 0
    top_recommendations: List[str] = Field(default_factory=list)
    theme_coverage: List[TopicCoverageThemeItem] = Field(default_factory=list)
    topic_focus: List[str] = Field(default_factory=list)
    matched_theme_topics: List[str] = Field(default_factory=list)
    summary: str = ""


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
    stale_after_days: int
    measurement_generated_at: datetime
    measurement_window_days: int
    measurement_stale_after_days: int
    total_snapshots: int
    stale_snapshots: int
    recent_snapshots: int
    stale_ratio: float
    freshest_snapshot_at: Optional[datetime] = None
    oldest_snapshot_at: Optional[datetime] = None
    stale_data_flag: bool = False
    insufficient_history_flag: bool = False
    readiness_threshold: float = 0.25
    items: List[RetentionSnapshotOperationItem]


class RetentionSnapshotTrendReport(BaseModel):
    generated_at: datetime
    window_days: int
    trends: List[RetentionSnapshotTrendItem]


class RetentionHealthTypeCount(BaseModel):
    snapshot_type: str
    count: int


class RetentionHealthReport(BaseModel):
    generated_at: datetime
    user_id: int
    window_days: int
    total_snapshots: int
    average_loyalty_score: float
    average_churn_risk_score: float
    dominant_snapshot_type: Optional[str] = None
    counts_by_type: List[RetentionHealthTypeCount] = Field(default_factory=list)
    coverage: float = 0.0
    snapshot_report: RetentionSnapshotReport
    trend_report: RetentionSnapshotTrendReport


class RetentionDashboard(BaseModel):
    generated_at: datetime
    window_days: int
    summary: InteractionSummary
    churn_prediction: ChurnPrediction
    snapshot_report: RetentionSnapshotReport
    snapshot_delta: RetentionSnapshotDelta
    snapshot_trends: RetentionSnapshotTrendReport
    snapshot_operations_report: Optional[RetentionSnapshotOperationsReport] = None
    topic_signal_report: Optional[RetentionTopicSignalReport] = None
    topic_signal_detail: Optional[RetentionTopicSignalDetail] = None
    topic_theme_coverage: List[TopicThemeCoverageItem] = Field(default_factory=list)
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


class RetentionMaintenancePreviewItem(BaseModel):
    snapshot_type: str
    count: int


class RetentionMaintenancePreview(BaseModel):
    generated_at: datetime
    user_id: int
    window_days: int
    keep: int
    total_snapshots: int
    retained_snapshots: int
    stale_snapshots: int
    stale_by_type: List[RetentionMaintenancePreviewItem]


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
    risk_level: str
    overview: str


class RetentionSnapshotOperationsCompliance(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    compliance: str
    risk_level: str
    overview: str


class RetentionSnapshotOperationsPosture(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    posture: str
    risk_level: str
    overview: str


class RetentionSnapshotOperationsAutomation(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    automation: str
    risk_level: str
    overview: str

    @property
    def automation_ready(self) -> str:
        return self.automation


class RetentionSnapshotOperationsExecutionState(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    execution_state: str
    risk_level: str
    overview: str


class RetentionSnapshotOperationsLaunchReadiness(BaseModel):
    generated_at: datetime
    window_days: int
    stale_after_days: int
    readiness: str
    risk_level: str
    overview: str

    @property
    def launch_readiness(self) -> str:
        return self.readiness


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


class RecoveryOutcomeItem(BaseModel):
    id: int
    recovery_readiness: str
    dissatisfaction_score: float
    acknowledged: bool
    acknowledged_at: Optional[datetime]
    source: str
    follow_up_count: int = 0
    complaint_recurrence_count: int = 0
    time_to_acknowledge_minutes: Optional[float] = None


class RecoveryOutcomeReport(BaseModel):
    generated_at: datetime
    window_days: int
    summary: InteractionSummary
    dissatisfaction: DissatisfactionRecoveryReport
    retention_recommendation: RetentionSnapshotRecommendation
    action_plan: RetentionSnapshotActionPlan
    recovery_outcome: RecoveryOutcomeItem
    churn_risk: str
    recovery_attempts: int
    recovery_acknowledged: bool
    complaint_recurrence_count: int
    time_to_acknowledge_minutes: Optional[float] = None


class RecoveryOutcomeAggregateItem(BaseModel):
    recovery_readiness: str
    attempts: int
    acknowledged: int


class RecoveryOutcomeAggregateReport(BaseModel):
    generated_at: datetime
    window_days: int
    total_attempts: int
    total_acknowledged: int
    total_complaint_recurrences: int
    items: list[RecoveryOutcomeAggregateItem]


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