from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List
from enum import Enum

class ServiceTypeEnum(str, Enum):
    consultation = "consultation"
    delivery = "delivery"
    meeting = "meeting"
    project = "project"

class BookingStatusEnum(str, Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"

class BookingCreate(BaseModel):
    service_type: ServiceTypeEnum
    title: str
    description: Optional[str] = None
    scheduled_date: datetime

class BookingUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_date: Optional[datetime] = None
    status: Optional[BookingStatusEnum] = None

class BookingOut(BaseModel):
    id: int
    service_type: ServiceTypeEnum
    title: str
    description: Optional[str]
    scheduled_date: datetime
    status: BookingStatusEnum
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

class UserLogin(BaseModel):
    username: str
    password: str        

