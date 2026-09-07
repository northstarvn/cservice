from pydantic import BaseModel, field_validator, ConfigDict, EmailStr
from typing import Optional, List
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


class BookingAuditSummary(BaseModel):
    booking_id: int
    user_id: int
    total_events: int
    created_events: int
    status_updates: int
    latest_event_at: Optional[datetime] = None


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
    items: List[BookingEventOut]


class AnalyticsEventCount(BaseModel):
    event_type: str
    count: int


class AdminAnalyticsSummary(BaseModel):
    generated_at: datetime
    total_users: int
    total_bookings: int
    bookings_by_status: dict[str, int]
    booking_events_total: int
    booking_events_by_type: List[AnalyticsEventCount]
    recent_bookings: int
    recent_events: int


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


class BookingExportReport(BaseModel):
    generated_at: datetime
    total_bookings: int
    total_events: int
    bookings: List[BookingOut]
    events: List[BookingEventOut]

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