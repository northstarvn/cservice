from pydantic import BaseModel, field_validator, ConfigDict, EmailStr, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

class ServiceType(str, Enum):
    consultation = "consultation"
    delivery = "delivery"
    meeting = "meeting"
    project = "project"

class BookingStatus(str, Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"

def _normalize_enum(v, enum_cls):
    if v is None:
        return v
    if isinstance(v, enum_cls):
        return v
    if isinstance(v, str):
        # Try exact match first
        try:
            return enum_cls(v.lower())
        except ValueError:
            # Try name matching
            for member in enum_cls:
                if member.name.lower() == v.lower():
                    return member
            raise ValueError(f"Invalid {enum_cls.__name__}: {v}")
    raise ValueError(f"Invalid type for {enum_cls.__name__}: {type(v)}")

class BookingUpdate(BaseModel):
    service_type: Optional[ServiceType] = None
    title: Optional[str] = None
    details: Optional[str] = None
    scheduled_date: Optional[datetime] = None
    status: Optional[BookingStatus] = None

    @field_validator("service_type", mode="before")
    @classmethod
    def norm_service_type(cls, v):
        if v is None:
            return v
        return _normalize_enum(v, ServiceType)

    @field_validator("status", mode="before")
    @classmethod
    def norm_status(cls, v):
        if v is None:
            return v
        return _normalize_enum(v, BookingStatus)
    
class BookingCreate(BaseModel):
    service_type: ServiceType
    title: Optional[str] = None
    details: str = ""
    scheduled_date: datetime

    @field_validator("service_type", mode="before")
    @classmethod
    def norm_service_type(cls, v):
        return _normalize_enum(v, ServiceType)

class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    service_type: ServiceType
    title: Optional[str] = None
    details: str
    scheduled_date: datetime
    status: BookingStatus
    created_at: datetime
    updated_at: datetime
    control_posture: str = "observed"


class BookingAssignmentState(str, Enum):
    suggested = "suggested"
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"
    expired = "expired"
    reassigned = "reassigned"


class BookingAssignmentDecision(BaseModel):
    booking_id: int
    user_id: int
    room_id: int
    match_reason: str
    state: BookingAssignmentState
    source: str
    explanation: Optional[str] = None
    created_at: datetime
    is_current: bool = True


class BookingAssignmentReport(BaseModel):
    generated_at: datetime
    booking_id: int
    user_id: int
    current_state: BookingAssignmentState
    current_assignment_is_current: bool = True
    decisions: List[BookingAssignmentDecision]
    control_posture: str = "observed"


class BookingAssignmentCreate(BaseModel):
    booking_id: int
    room_id: int
    match_reason: str
    state: BookingAssignmentState = BookingAssignmentState.suggested
    source: str = "booking-service"
    explanation: Optional[str] = None


class BookingAssignmentRequest(BaseModel):
    room_id: Optional[int] = None
    match_reason: Optional[str] = None
    state: BookingAssignmentState = BookingAssignmentState.suggested
    source: str = "booking-service"
    explanation: Optional[str] = None


class BookingAssignmentUpdate(BaseModel):
    room_id: Optional[int] = None
    match_reason: Optional[str] = None
    state: Optional[BookingAssignmentState] = None
    source: Optional[str] = None
    explanation: Optional[str] = None


class BookingEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    booking_id: int
    user_id: int
    event_type: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    note: str
    created_at: datetime
    is_assignment_event: bool = False


class BookingAuditSummary(BaseModel):
    booking_id: int
    user_id: int
    total_events: int
    assignment_events: int = 0
    created_events: int
    status_updates: int
    event_type_counts: dict[str, int] = Field(default_factory=dict)
    latest_event_at: Optional[datetime] = None
    latest_event_note: Optional[str] = None
    recent_mutation_fields: List[str] = Field(default_factory=list)
    control_posture: str = "observed"


class BookingTransitionResult(BaseModel):
    booking: BookingOut
    event: BookingEventOut


class BookingHistoryItem(BaseModel):
    booking: BookingOut
    events: List[BookingEventOut]


class BookingHistoryReport(BaseModel):
    booking_id: int
    user_id: int
    current_status: BookingStatus
    event_count: int
    assignment_events: int = 0
    event_type_counts: dict[str, int] = Field(default_factory=dict)
    items: List[BookingEventOut]
    control_posture: str = "observed"


class BookingSummary(BaseModel):
    booking_id: int
    user_id: int
    current_status: BookingStatus
    status_counts: dict[str, int] = Field(default_factory=dict)
    assignment_count: int = 0
    control_posture: str = "observed"


class BookingSummaryReport(BaseModel):
    generated_at: datetime
    booking_id: int
    user_id: int
    current_status: BookingStatus
    status_counts: dict[str, int] = Field(default_factory=dict)
    assignment_count: int = 0
    control_posture: str = "observed"


class BookingOperationReport(BaseModel):
    generated_at: datetime
    booking_id: int
    user_id: Optional[int] = None
    control_posture: str = "observed"
    summary: dict[str, object] = Field(default_factory=dict)
    timeline: dict[str, object] = Field(default_factory=dict)
    typed_summary: dict[str, object] = Field(default_factory=dict)
    typed_assignment_summary: dict[str, object] = Field(default_factory=dict)
    topic_context: str = ""
    topic_coverage_ratio: float = 0.0
    topic_portfolio_coverage: float = 0.0
    topic_theme_overlap: int = 0
    topic_signal_summary: str = ""
    topic_focus: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)


class BookingAssignmentSummary(BaseModel):
    booking_id: int
    user_id: int
    total_assignments: int
    current_assignments: int
    historical_assignments: int
    state_counts: dict[str, int]
    control_posture: str = "observed"


class BookingAssignmentReportPage(BaseModel):
    booking_id: int
    user_id: int
    total_assignments: int
    current_assignments: int = 0
    historical_assignments: int = 0
    state_counts: dict[str, int]
    source_counts: dict[str, int]
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    page: int
    per_page: int
    pages: int
    items: List[BookingAssignmentReport]
    control_posture: str = "observed"


class BookingAssignmentHistoryReport(BaseModel):
    booking_id: int
    user_id: int
    event_count: int
    assignment_events: int = 0
    event_type_counts: dict[str, int] = Field(default_factory=dict)
    items: List[BookingEventOut]
    control_posture: str = "observed"


class BookingAssignmentHistorySummary(BaseModel):
    booking_id: int
    user_id: int
    event_count: int
    assignment_events: int = 0
    latest_event_at: Optional[datetime] = None
    latest_event_type: Optional[str] = None
    event_type_counts: dict[str, int] = Field(default_factory=dict)
    items: List[BookingEventOut] = Field(default_factory=list)
    control_posture: str = "observed"


class AnalyticsEventCount(BaseModel):
    event_type: str
    count: int


class AdminAnalyticsSummary(BaseModel):
    generated_at: datetime
    total_users: int
    total_bookings: int
    bookings_by_status: dict[str, int]
    booking_events_total: int
    assignment_events_total: int = 0
    assignment_event_ratio: float = 0.0
    booking_events_by_type: List[AnalyticsEventCount]
    recent_bookings: int
    recent_events: int
    control_posture: str = "observed"


class BookingEventFilterSummary(BaseModel):
    generated_at: datetime
    total_events: int
    page: int
    per_page: int
    pages: int
    event_type: Optional[str] = None
    booking_id: Optional[int] = None
    user_id: Optional[int] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    statuses: dict[str, int]
    events: List[BookingEventOut]
    assignment_events: int = 0
    assignment_event_ratio: float = 0.0


class BookingExportReport(BaseModel):
    generated_at: datetime
    total_bookings: int
    total_events: int
    assignment_events_total: int = 0
    assignment_event_ratio: float = 0.0
    bookings: List[BookingOut]
    events: List[BookingEventOut]
    control_posture: str = "observed"

class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    email: str
    full_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    is_admin: bool = False

class PaginatedBookings(BaseModel):
    items: List[BookingOut]
    total: int
    page: int
    per_page: int
    pages: int

class UserBase(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None

class UserCreate(UserBase):
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    expires_in: int

class TokenData(BaseModel):
    username: str

class UserLogin(BaseModel):
    username: str
    password: str
    locale: Optional[str] = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class PasswordChangeResult(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Session lifecycle / machine credentials (additive)
#
# ``Token`` and ``PasswordChangeResult`` above keep their historical shape;
# everything here is new surface for session refresh, logout, API keys and
# non-mutating password feedback.
# ---------------------------------------------------------------------------


class TokenRefreshRequest(BaseModel):
    refresh_token: str
    # Rotation is the default: a presented refresh token is burned and a new one
    # is handed back, so a stolen token is single-use.
    rotate_refresh_token: bool = True
    locale: Optional[str] = None


class TokenRefreshResult(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: Optional[str] = None
    refresh_expires_in: int = 0
    rotated: bool = False
    previous_jti_revoked: bool = False
    locale: Dict[str, Any] = Field(default_factory=dict)


class SessionOut(BaseModel):
    jti: str
    token_type: str = "access"
    subject: str = ""
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked: bool = False
    active: bool = False


class SessionInventoryOut(BaseModel):
    subject: str
    sessions: List[SessionOut]
    active_sessions: int = 0
    revoked_sessions: int = 0
    mechanism: str = "jti deny-list, TTL-bounded"


class LogoutRequest(BaseModel):
    # Omit to revoke the credential that authenticated the call.
    token: Optional[str] = None
    all_sessions: bool = False


class LogoutResult(BaseModel):
    revoked: bool = False
    scope: str = "token"
    newly_revoked: int = 0
    token_type: Optional[str] = None
    message: str


class ApiKeyCreate(BaseModel):
    name: str
    scopes: List[str] = Field(default_factory=list)
    expires_in_days: Optional[int] = None


class ApiKeyOut(BaseModel):
    key_id: str
    name: str
    subject: str
    scopes: List[str] = Field(default_factory=list)
    hash_prefix: str = ""
    created_at: str = ""
    expires_at: Optional[str] = None
    revoked: bool = False
    # Only ever populated on the issuing response — the secret is not stored.
    plaintext: Optional[str] = None


class ApiKeyListOut(BaseModel):
    subject: str
    keys: List[ApiKeyOut]
    total: int = 0
    active: int = 0
    revoked: int = 0


class ApiKeyRevokeResult(BaseModel):
    key_id: str
    revoked: bool = False
    message: str


class PasswordFeedbackRequest(BaseModel):
    candidate: str
    # Optional: lets the advisor flag usernames embedded in the candidate.
    username: Optional[str] = None


class PasswordFeedbackOut(BaseModel):
    accepted: bool = False
    score: int = 0
    entropy_bits: float = 0.0
    adjusted_bits: float = 0.0
    factors: List[str] = Field(default_factory=list)
    penalties: List[str] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)
    advice: List[str] = Field(default_factory=list)
    reused: bool = False
    contains_username: bool = False
    policy: Dict[str, Any] = Field(default_factory=dict)


class PasswordPolicyOut(BaseModel):
    policy: Dict[str, Any]
    history_depth: int = 0
    requirements: List[str] = Field(default_factory=list)
    advice: List[str] = Field(default_factory=list)
    scale: List[int] = Field(default_factory=lambda: [0, 4])


class SecurityPostureOut(BaseModel):
    subject: str
    control_posture: str = "observed"
    step_up_level: str = "none"
    auth_method: str = "user"
    active_sessions: int = 0
    active_api_keys: int = 0
    expired_api_keys: int = 0
    highest_known_step_up: str = "none"
    signed_in_as_admin: bool = False
    recommendations: List[Dict[str, Any]] = Field(default_factory=list)


class StepUpRequest(BaseModel):
    level: str = "loa2"
    method: str = "mfa"


class StepUpResult(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    level: str = "none"
    granted_level: str = "none"
    satisfied: bool = False
    methods: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Customer policy scoring
# ---------------------------------------------------------------------------

class CustomerPolicyScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    system_score: float
    customer_score: float
    access_score: float
    interest_score: float
    closeness_score: float
    community_closeness_score: float
    policy_tier: str
    control_posture: str
    source: str
    summary: str
    created_at: datetime
    updated_at: datetime


class CustomerPolicyAccessOut(BaseModel):
    user_id: int
    functionality: str
    required_tier: str
    effective_required_tier: str
    allowed: bool
    policy_tier: str
    control_posture: str
    access_score: float
    customer_score: float
    system_score: float


class CustomerPolicyHealthSummaryOut(BaseModel):
    generated_at: datetime
    policy_tier: str
    control_posture: str
    dominant_signal: str
    weak_points: List[dict[str, int | float | str]]
    strong_points: List[dict[str, int | float | str]]
    balance_index: float


class CustomerPolicyRecommendationOut(BaseModel):
    priority: str
    area: str
    recommendation: str
    evidence: str


class CustomerPolicyDecisionSummaryOut(BaseModel):
    generated_at: datetime
    user_id: int
    functionality: str
    required_tier: str
    decision: CustomerPolicyAccessOut
    health: CustomerPolicyHealthSummaryOut
    recommendations: List[CustomerPolicyRecommendationOut]
    summary: str
    topic_context: str = ""


class CustomerPolicyDecisionReportOut(BaseModel):
    policy_decision: CustomerPolicyDecisionSummaryOut
    policy_score: CustomerPolicyScoreOut
    control_posture: str
    policy_tier: str
    topic_context: str = ""
    topic_analysis: Optional[dict[str, object]] = None


# ---------------------------------------------------------------------------
# Topic intelligence
# ---------------------------------------------------------------------------

class TopicSelectionCreate(BaseModel):
    topic: str
    source: str = "chat"
    rationale: str = ""
    confidence: float = 0.0


class TopicSelectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    topic: str
    source: str
    rationale: str
    confidence: float
    is_current: bool = True
    created_at: datetime
    updated_at: datetime


class TopicSelectionReport(BaseModel):
    generated_at: datetime
    user_id: int
    topic: str
    source: str
    rationale: str
    confidence: float
    is_current: bool = True
    topic_focus: List[str] = Field(default_factory=list)


class TopicSelectionHistoryReport(BaseModel):
    generated_at: datetime
    user_id: int
    items: List[TopicSelectionOut]
    topic_focus: List[str] = Field(default_factory=list)
    summary: str = ""


class TopicCatalogItem(BaseModel):
    topic: str
    source: str
    rationale: str
    confidence: float
    keywords: List[str]


class TopicTopicMapItem(BaseModel):
    topic: str
    keywords: List[str]
    related_topics: List[str]
    theme: Optional[str] = None
    sector: Optional[str] = None


class TopicTaxonomyReport(BaseModel):
    generated_at: datetime
    total_topics: int
    total_themes: int
    sectors: List[str]
    topic_map: List[TopicTopicMapItem]
    theme_map: List[dict[str, object]]
    summary: str
    topic_focus: List[str] = Field(default_factory=list)


class TopicCatalogReport(BaseModel):
    generated_at: datetime
    total_topics: int
    items: List[TopicCatalogItem]
    top_prefixes: List[dict[str, int | str]] = []
    topic_focus: List[str] = Field(default_factory=list)
    summary: str = ""


class TopicThemeItem(BaseModel):
    theme: str
    topic_count: int
    topics: List[str]


class TopicThemeReport(BaseModel):
    generated_at: datetime
    total_themes: int
    themes: List[TopicThemeItem]
    topic_focus: List[str] = Field(default_factory=list)
    summary: str = ""


class TopicRecommendationItem(BaseModel):
    topic: str
    source: str
    confidence: float
    rationale: str


class TopicRecommendationReport(BaseModel):
    generated_at: datetime
    topic: Optional[str]
    primary_topic: Optional[str]
    coverage_ratio: float = 0.0
    match_count: int = 0
    recommendations: List[TopicRecommendationItem]
    matched_themes: List[str] = Field(default_factory=list)
    theme_coverage: List[Dict[str, Any]] = Field(default_factory=list)
    topic_focus: List[str] = Field(default_factory=list)
    summary: str


class TopicIntelligenceReport(BaseModel):
    generated_at: datetime
    topic: Optional[str]
    matched_keywords: List[str]
    suggested_topics: List[TopicCatalogItem]
    coverage_ratio: float = 0.0
    match_count: int = 0
    catalog_size: int = 0
    topic_focus: List[str] = Field(default_factory=list)
    summary: str


class TopicWorkspaceReport(BaseModel):
    generated_at: datetime
    user_id: int
    topic: Optional[str]
    catalog: TopicCatalogReport
    taxonomy: TopicTaxonomyReport
    themes: TopicThemeReport
    intelligence: TopicIntelligenceReport
    coverage: dict[str, object]
    portfolio: dict[str, object]
    recommendations: TopicRecommendationReport
    selection_history: TopicSelectionHistoryReport
    richness_score: float = 0.0
    topic_focus: List[str] = Field(default_factory=list)


class TopicSearchResult(BaseModel):
    topic: str
    score: float
    matched_keywords: List[str] = Field(default_factory=list)
    theme: Optional[str] = None
    sector: Optional[str] = None


class TopicIntelligenceOverview(BaseModel):
    generated_at: datetime
    user_id: int
    topic: Optional[str] = None
    workspace: TopicWorkspaceReport
    portfolio: Dict[str, Any] = Field(default_factory=dict)
    coverage: Dict[str, Any] = Field(default_factory=dict)
    recommendations: Dict[str, Any] = Field(default_factory=dict)
    suggested_topics: List[TopicSearchResult] = Field(default_factory=list)
    catalog_size: int = 0
    matched_topic_count: int = 0
    suggestion_count: int = 0
    summary: str
    topic_focus: List[str] = Field(default_factory=list)


class TopicSuggestionReport(BaseModel):
    generated_at: datetime
    query: str
    total_results: int
    suggested_topics: List[TopicSearchResult]
    catalog_size: int = 0
    topic_focus: List[str] = Field(default_factory=list)
    summary: str


class TopicSearchReport(BaseModel):
    generated_at: datetime
    query: str
    total_results: int
    page: int = 1
    per_page: int = 5
    total_pages: int = 1
    items: List[TopicSearchResult]
    catalog_size: int = 0
    topic_focus: List[str] = Field(default_factory=list)
    summary: str

# --- Request governance & capacity planning ------------------------------------
#
# `/topics/search`, `/topics/suggestions`, `/topics/ranked`, `/topics/drift` and
# friends take caller-supplied numbers (`limit`, `page`, `per_page`, `window`)
# and free text (`query`, `topic`) that reach a service with no bounds of its
# own. These contracts back the additive governance surface over those inputs:
# what a parameter is allowed to be, which route requires what, how large a
# response may get, and what a request would do if it were planned offline.
#
# Nothing here changes an existing route. `POST /topics/validate` is already
# advisory for a *topic*; `GET /topics/plan` is advisory for a *request*.


class TopicParamCoercion(BaseModel):
    """One parameter that was accepted but not exactly as supplied.

    A clamp is recorded rather than applied silently, so a caller that asked for
    ``per_page=5000`` is told it received 100 rather than discovering a short
    page with no explanation.
    """

    param: str
    supplied: Any = None
    resolved: Any = None
    rule: str  # clamped | truncated | defaulted | coerced_type
    reason: str = ""


class TopicParamViolation(BaseModel):
    """One parameter that could not be repaired and was refused."""

    param: str
    supplied: Any = None
    reason: str  # out_of_range | too_long | wrong_type | empty
    bound: str = ""
    detail: str = ""


class TopicParamBound(BaseModel):
    """The declared contract for one request parameter."""

    param: str
    kind: str  # int | float | str | bool
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    default: Any = None
    max_length: Optional[int] = None
    required: bool = False
    on_violation: str = "clamp"  # clamp | reject
    used_by: List[str] = Field(default_factory=list)
    description: str = ""


class TopicParamBoundsReport(BaseModel):
    generated_at: datetime
    count: int
    bounds: List[TopicParamBound] = Field(default_factory=list)
    clamp_params: List[str] = Field(default_factory=list)
    reject_params: List[str] = Field(default_factory=list)
    note: str = ""


class TopicCachePolicy(BaseModel):
    """How a route's response may be cached, and by whom."""

    cache_class: str
    max_age: int
    stale_while_revalidate: int
    private: bool
    varies_on: List[str] = Field(default_factory=list)
    routes: List[str] = Field(default_factory=list)
    description: str = ""


class TopicRoutePolicy(BaseModel):
    """One row of the route policy table."""

    route_id: str
    method: str
    path: str
    handler: str
    auth: str  # none | user
    scope: str  # catalog | user | user_write | advisory | governance
    cache: str
    rate_tier: str
    cost_weight: float
    max_rows: int
    mutating: bool = False
    observed: bool = False
    params: List[str] = Field(default_factory=list)
    description: str = ""


class TopicRoutePolicyReport(BaseModel):
    generated_at: datetime
    total: int
    observed: int
    undeclared: List[str] = Field(default_factory=list)
    stale: List[str] = Field(default_factory=list)
    policies: List[TopicRoutePolicy] = Field(default_factory=list)
    by_scope: Dict[str, int] = Field(default_factory=dict)
    by_cache: Dict[str, int] = Field(default_factory=dict)
    by_tier: Dict[str, int] = Field(default_factory=dict)
    note: str = ""


class TopicResponseLimit(BaseModel):
    """A cap on one list-valued field of one report."""

    report: str
    field: str
    cap: int
    overflow: str  # truncate | reject | report_only
    routes: List[str] = Field(default_factory=list)
    description: str = ""


class TopicResponseTruncation(BaseModel):
    """What capping a payload actually did, so the caller can say so."""

    field: str
    original_length: int
    kept_length: int
    dropped: int
    cap: int
    overflow: str


class TopicResponseLimitReport(BaseModel):
    generated_at: datetime
    total: int
    limits: List[TopicResponseLimit] = Field(default_factory=list)
    cache_policies: List[TopicCachePolicy] = Field(default_factory=list)
    max_cap: int = 0
    total_capped_fields: int = 0
    cache_classes: List[str] = Field(default_factory=list)
    note: str = ""


class TopicCapacityEntry(BaseModel):
    """Cost model for one route: what it costs to serve."""

    route_id: str
    slo_tier: str
    latency_budget_ms: int
    db_queries: int
    catalog_scans: int
    rate_tier: str
    cost_weight: float
    cache: str
    cacheable: bool = True
    max_rows: int = 0
    note: str = ""


class TopicCapacityReport(BaseModel):
    generated_at: datetime
    total: int
    by_slo_tier: Dict[str, int] = Field(default_factory=dict)
    entries: List[TopicCapacityEntry] = Field(default_factory=list)
    heaviest: List[str] = Field(default_factory=list)
    total_db_queries: int = 0
    total_catalog_scans: int = 0
    aggregate_cost_weight: float = 0.0
    cacheable_share: float = 0.0
    note: str = ""


class TopicAdmissionDecision(BaseModel):
    """May this request proceed, and if not, why."""

    allowed: bool = True
    reason: str = "ok"  # ok | param_out_of_range | rate_limited | unknown_route
    rate_tier: str = ""
    capacity: int = 0
    remaining: int = 0
    retry_after_seconds: int = 0
    violations: List[TopicParamViolation] = Field(default_factory=list)
    detail: str = ""


class TopicRequestPlanReport(BaseModel):
    """What ``GET /topics/plan`` says a request *would* do. Writes nothing."""

    generated_at: datetime
    route_id: str
    known: bool
    method: str = ""
    path: str = ""
    auth: str = ""
    scope: str = ""
    cache: str = ""
    rate_tier: str = ""
    cost_weight: float = 0.0
    max_rows: int = 0
    supplied: Dict[str, Any] = Field(default_factory=dict)
    resolved: Dict[str, Any] = Field(default_factory=dict)
    unused: List[str] = Field(default_factory=list)
    defaults_applied: List[str] = Field(default_factory=list)
    coercions: List[TopicParamCoercion] = Field(default_factory=list)
    violations: List[TopicParamViolation] = Field(default_factory=list)
    page: Dict[str, Any] = Field(default_factory=dict)
    admission: TopicAdmissionDecision = Field(default_factory=TopicAdmissionDecision)
    caps: List[TopicResponseLimit] = Field(default_factory=list)
    db_queries: int = 0
    latency_budget_ms: int = 0
    slo_tier: str = ""
    summary: str = ""


class TopicRequestSample(BaseModel):
    """One recorded request, in the shape a capacity review needs."""

    route_id: str
    method: str = ""
    scope: str = ""
    rate_tier: str = ""
    cost_weight: float = 0.0
    admitted: bool = True
    reason: str = "ok"
    coercions: int = 0
    violations: int = 0
    page: int = 1
    per_page: int = 5
    observed_at: datetime


class TopicRequestAnalyticsReport(BaseModel):
    """What the live routes have actually been asked for."""

    generated_at: datetime
    tracked: bool
    capacity: int
    recorded: int
    admitted: int
    refused: int
    dropped: int
    record_errors: int
    by_route: Dict[str, int] = Field(default_factory=dict)
    by_reason: Dict[str, int] = Field(default_factory=dict)
    by_tier: Dict[str, int] = Field(default_factory=dict)
    coerced_params: Dict[str, int] = Field(default_factory=dict)
    refused_params: Dict[str, int] = Field(default_factory=dict)
    heaviest_routes: List[str] = Field(default_factory=list)
    recent: List[TopicRequestSample] = Field(default_factory=list)
    note: str = ""


class TopicRouteDriftReport(BaseModel):
    """Routes the table and the router disagree about."""

    generated_at: datetime
    declared: int
    observed: int
    undeclared: List[str] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)
    method_mismatches: List[str] = Field(default_factory=list)
    path_mismatches: List[str] = Field(default_factory=list)
    handler_mismatches: List[str] = Field(default_factory=list)
    in_sync: bool = False
    note: str = ""


class TopicPolicyValidationReport(BaseModel):
    """Errors and warnings from checking the governance tables against each other."""

    generated_at: datetime
    errors: int = 0
    warnings: int = 0
    error_list: List[str] = Field(default_factory=list)
    warning_list: List[str] = Field(default_factory=list)
    routes: int = 0
    params: int = 0
    limits: int = 0
    note: str = ""


# ---------------------------------------------------------------------------
# Sign-in methods, device recognition and connected storage
# ---------------------------------------------------------------------------
#
# Three response families below. Two conventions run through all of them:
#
# * A recognition verdict reports *contributions*, never the raw signals behind
#   them. `signal` and `contribution` are safe to log; a device id is not.
# * Nothing in these schemas can carry a credential. Tokens appear exactly
#   once, in a request to create them, and are never echoed by a list endpoint.

class RecognitionContributionOut(BaseModel):
    """One signal's share of a recognition score, without its value."""

    signal: str
    weight: float
    contribution: float = 0.0
    agrees: bool = False
    present: bool = False
    mismatch_penalty: float = 0.0


class RecognitionVerdictOut(BaseModel):
    """How much proof a login attempt is required to present.

    ``required_credential`` is what the caller must supply next;
    ``refresh_without_challenge`` says whether a live session may extend itself.
    Neither field means the request has been authenticated -- by construction
    this verdict never authorises anything on its own.
    """

    score: float = 0.0
    policy_id: str = "unfamiliar"
    label: str = ""
    required_credential: str = "password"
    required_assurance: str = "loa2"
    challenge: bool = True
    refresh_without_challenge: bool = False
    reason: str = ""
    version: str = ""
    contributions: List[RecognitionContributionOut] = Field(default_factory=list)
    known_signals: int = 0
    total_signals: int = 0
    trusted_blocked_reason: str = ""
    note: str = (
        "recognition decides how much proof to demand, never whether to accept. "
        "Every policy still requires a credential."
    )


class AuthMethodOut(BaseModel):
    """One login method, as this user can actually use it right now."""

    method: str
    label: str
    assurance: str
    amr: List[str] = Field(default_factory=list)
    enrollment_required: bool = False
    enabled_by_default: bool = True
    revocable: bool = True
    channel: str = ""
    rate_tier: str = "interactive"
    description: str = ""
    caveat: Optional[str] = None
    usable: bool = False
    enrolled: bool = False
    setup_needed: Optional[str] = None
    bootstrap: bool = False
    assurance_rank: int = 0


class RefusedMethodOut(BaseModel):
    """A method pattern deliberately not offered, and why."""

    pattern: str
    label: str
    why_not: str


class AuthMethodListOut(BaseModel):
    generated_at: datetime
    version: str = ""
    methods: List[AuthMethodOut] = Field(default_factory=list)
    usable_now: List[str] = Field(default_factory=list)
    enrolled: List[str] = Field(default_factory=list)
    weakest_first: bool = True
    refusal_policy: str = ""
    refused: List[RefusedMethodOut] = Field(default_factory=list)
    summary: str = ""


class OtpRequestIn(BaseModel):
    """Ask for a one-time code.

    The channel is fixed by which endpoint is called rather than chosen here:
    a client that can pick its own channel would be able to route an email OTP
    to itself and skip the inbox.
    """

    username: str = ""
    device_id: Optional[str] = None
    reason: str = "login"


class OtpRequestOut(BaseModel):
    """Sent, deliberately without the code.

    `user_id` and `email` are omitted unconditionally -- including when the
    account does not exist -- because a response that differs between a known
    and an unknown username is a free account-enumeration oracle.
    """

    sent: bool = False
    expires_in_seconds: int = 0
    max_attempts: int = 0
    reason: str = ""


class OtpVerifyIn(BaseModel):
    username: str = ""
    code: str = ""
    device_id: Optional[str] = None
    method: str = "email_otp"


class LoginWithDeviceIn(BaseModel):
    """Sign in with a trusted-device token.

    ``device_id`` is not optional here and that is the security property rather
    than an oversight: a token is only ever valid for the device digest it was
    minted against, so a caller who omits this is describing a login that could
    not succeed. Leaving the field off the schema would have made that a runtime
    surprise instead of a request the API refuses to describe.
    """

    username: str = ""
    device_token: str = ""
    device_id: str


class TrustedDeviceCreated(BaseModel):
    """The one response that carries a device token. Never returned twice."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = 0
    device_id: int = 0
    label: str = ""
    device_token: str = ""
    expires_at: datetime
    recognition: Optional[RecognitionVerdictOut] = None
    note: str = (
        "The device token is shown once and stored only as a hash. Trusting a "
        "device never lowers the bar for the login that creates it."
    )


class TrustedDeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str = ""
    created_at: datetime
    last_seen_at: datetime
    trusted_at: Optional[datetime] = None
    trust_expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    use_count: int = 0
    trusted: bool = False
    label_hint: str = ""


class DeviceListOut(BaseModel):
    generated_at: datetime
    devices: List[TrustedDeviceOut] = Field(default_factory=list)
    trusted_count: int = 0
    revoked_count: int = 0
    note: str = ""


class IdentityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    identifier_hint: str = ""
    is_primary: bool = False
    is_verified: bool = False
    verified_at: Optional[datetime] = None
    verification_method: str = ""
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    contact_consent: bool = False
    source: str = ""


class IdentityListOut(BaseModel):
    generated_at: datetime
    identities: List[IdentityOut] = Field(default_factory=list)
    verified_count: int = 0
    primary_hint: str = ""
    note: str = ""


class IdentityLinkIn(BaseModel):
    provider: str = "email"
    identifier: str = ""
    make_primary: bool = False
    contact_consent: bool = False


class IdentityLinkOut(BaseModel):
    identity: IdentityOut
    verification_required: bool = True
    reason: str = ""


class StorageProviderOut(BaseModel):
    provider: str
    label: str
    configured: bool = False
    supports_pkce: bool = False
    default_scope: str = ""
    scopes: List[Dict[str, Any]] = Field(default_factory=list)
    docs: Optional[str] = None
    notes: Optional[str] = None


class StorageProviderListOut(BaseModel):
    generated_at: datetime
    providers: List[StorageProviderOut] = Field(default_factory=list)
    configured_count: int = 0
    env_prefix: str = "STORAGE_"
    token_cipher: str = ""
    note: str = ""


class StorageConnectIn(BaseModel):
    provider: str
    scope: Optional[str] = None
    # Full access is a real capability a user can grant. It is not the default
    # and it is not reachable without this flag, which is what stops the broad
    # scope being granted by a client that did not mean to.
    confirm_broad_scope: bool = False
    label: str = ""


class StorageAuthorizeOut(BaseModel):
    provider: str
    authorize_url: str
    scope: str = ""
    broad_scope: bool = False
    requires_confirmation: bool = False
    grants: str = ""
    state: str = ""
    note: str = ""


class StorageCallbackIn(BaseModel):
    provider: str
    code: str = ""
    state: str = ""
    error: Optional[str] = None


class StorageConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    label: str = ""
    status: str = "active"
    scopes: List[str] = Field(default_factory=list)
    requested_scopes: List[str] = Field(default_factory=list)
    broad_scope_confirmed: bool = False
    healthy: bool = False
    problems: List[str] = Field(default_factory=list)
    connected_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoked_reason: str = ""


class StorageConnectionListOut(BaseModel):
    generated_at: datetime
    connections: List[StorageConnectionOut] = Field(default_factory=list)
    healthy_count: int = 0
    unhealthy_count: int = 0
    note: str = ""
