from pydantic import BaseModel, EmailStr
from datetime import datetime
from typing import Optional, List
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

class BookingCreate(BaseModel):
    service_type: ServiceType
    title: str
    details: Optional[str] = None
    scheduled_date: datetime

class BookingUpdate(BaseModel):
    title: Optional[str] = None
    details: Optional[str] = None
    scheduled_date: Optional[datetime] = None
    status: Optional[BookingStatus] = None

class BookingOut(BaseModel):
    id: int
    service_type: ServiceType
    title: str
    details: Optional[str]
    scheduled_date: datetime
    status: BookingStatus
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True

class BookingList(BaseModel):
    bookings: List[BookingOut]
    total: int

class UserBase(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None

class UserCreate(UserBase):
    password: str

class UserOut(UserBase):
    id: int
    model_config = {"from_attributes": True}

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: str

class BookingBase(BaseModel):
    service_type: str
    details: Optional[str] = None
    scheduled_date: datetime

class PaginatedBookings(BaseModel):
    items: List[BookingOut]
    total: int
    page: int
    per_page: int
    pages: int

    class Config:
        from_attributes = True

class UserLogin(BaseModel):
    username: str
    password: str        

class Token(BaseModel):
    access_token: str
    token_type: str    