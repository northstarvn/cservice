from pydantic import BaseModel, EmailStr
from datetime import datetime
from typing import List, Optional
import enum

class BookingStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"

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
    scheduled_for: datetime

class BookingCreate(BookingBase):
    pass

class BookingUpdate(BaseModel):
    service_type: Optional[str] = None
    details: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    status: Optional[BookingStatus] = None

class BookingOut(BookingBase):
    id: int
    status: BookingStatus
    created_at: datetime
    updated_at: datetime
    user_id: int
    model_config = {"from_attributes": True}