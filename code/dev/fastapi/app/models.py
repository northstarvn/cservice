from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum, func
from sqlalchemy.orm import relationship
from app.db import Base
import enum

class BookingStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String(100))
    email = Column(String(100), unique=True, index=True)
    bookings = relationship("Booking", back_populates="user")

class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    service_type = Column(String(50), nullable=False)
    details = Column(String(255))
    scheduled_for = Column(DateTime, nullable=False)
    status = Column(Enum(BookingStatus), default=BookingStatus.pending, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="bookings")