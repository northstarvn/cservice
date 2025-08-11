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

class ServiceType(str, Enum):
    cleaning = "cleaning"
    maintenance = "maintenance"
    consultation = "consultation"

def _normalize_enum(v, enum_cls):
    if v is None:
        return v
    if isinstance(v, enum_cls):
        return v
    if isinstance(v, str):
        low = v.lower()
        if low in enum_cls._value2member_map_:
            return enum_cls(low)
    return v

# BookingCreate should NOT have status (server sets it)
class BookingCreate(BaseModel):
    service_type: ServiceType
    title: Optional[str] = None
    details: str
    scheduled_date: datetime

    @field_validator("service_type", mode="before")
    @classmethod
    def norm_service_type(cls, v):
        return _normalize_enum(v, ServiceType)

# Allow partial updates; normalize enums
class BookingUpdate(BaseModel):
    service_type: Optional[ServiceType] = None
    title: Optional[str] = None
    details: Optional[str] = None
    scheduled_date: Optional[datetime] = None
    status: Optional[BookingStatus] = None

    @field_validator("service_type", mode="before")
    @classmethod
    def norm_service_type(cls, v):
        return _normalize_enum(v, ServiceType)

    @field_validator("status", mode="before")
    @classmethod
    def norm_status(cls, v):
        return _normalize_enum(v, BookingStatus)

# REQUIRED: enable ORM attribute reading
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

# If you return User objects elsewhere, also add from_attributes to UserOut
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    created_at: datetime
    updated_at: datetime

# Paginated wrapper DOES NOT need from_attributes
class PaginatedBookings(BaseModel):
    items: List[BookingOut]
    total: int
    page: int
    per_page: int
    pages: int

class BookingList(BaseModel):
    bookings: List[BookingOut]
    total: int

class UserBase(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None

class UserCreate(UserBase):
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: str

class UserLogin(BaseModel):
    username: str
    password: str        

class Token(BaseModel):
    access_token: str
    token_type: str    