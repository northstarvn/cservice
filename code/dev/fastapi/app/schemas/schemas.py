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


class CustomerPolicyDecisionReportOut(BaseModel):
    policy_decision: "CustomerPolicyDecisionSummaryOut"
    policy_score: CustomerPolicyScoreOut
    control_posture: str
    policy_tier: str
    topic_context: str = ""
    topic_analysis: Optional[dict[str, object]] = None


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