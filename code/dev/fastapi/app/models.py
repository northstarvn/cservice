from sqlalchemy import Column, Integer, String, DateTime, Text, Float, ForeignKey, Enum as SAEnum, Boolean, func, CheckConstraint
from sqlalchemy.orm import relationship
import enum
from app.db import Base  # Import Base from db.py instead of creating new one


class TimestampMixin:
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def touch(self):
        self.updated_at = func.now()



class ChatHistory(Base, TimestampMixin):
    __tablename__ = "chat_history"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    message = Column(Text, nullable=False)
    response = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class InteractionSignal(Base, TimestampMixin):
    __tablename__ = "interaction_signals"
    __table_args__ = (
        CheckConstraint("score >= 0", name="ck_interaction_signals_score_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(50), nullable=False, default="chat")
    area = Column(String(100), nullable=False, index=True)
    priority = Column(String(20), nullable=False, default="medium")
    score = Column(Float, nullable=False, default=0.0)
    evidence = Column(Text, nullable=False, default="")
    recommendation = Column(Text, nullable=False)


class RetentionSnapshot(Base, TimestampMixin):
    __tablename__ = "retention_snapshots"
    __table_args__ = (
        CheckConstraint("loyalty_score >= 0", name="ck_retention_snapshots_loyalty_score_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_type = Column(String(50), nullable=False, index=True)
    window_days = Column(Integer, nullable=False, default=30)
    loyalty_score = Column(Float, nullable=False, default=0.0)
    churn_risk = Column(String(20), nullable=False, default="low")
    lifecycle_stage = Column(String(50), nullable=False, default="new")
    summary_json = Column(Text, nullable=False, default="{}")


class BookingEvent(Base, TimestampMixin):
    __tablename__ = "booking_events"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(50), nullable=False, index=True)
    from_status = Column(String(20), nullable=True)
    to_status = Column(String(20), nullable=True)
    note = Column(Text, nullable=False, default="")

class BookingStatus(enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"

class ServiceType(enum.Enum):
    consultation = "consultation"
    delivery = "delivery"
    meeting = "meeting"
    project = "project"

class User(Base, TimestampMixin):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=False)
    full_name = Column(String(100))
    hashed_password = Column(String(255), nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    bookings = relationship("Booking", back_populates="user", cascade="all, delete-orphan")
    interaction_signals = relationship(
        "InteractionSignal",
        backref="user",
        cascade="all, delete-orphan",
    )
    chat_history = relationship(
        "ChatHistory",
        passive_deletes=True,
        backref="user",
    )
    retention_snapshots = relationship(
        "RetentionSnapshot",
        backref="user",
        cascade="all, delete-orphan",
    )
    booking_events = relationship(
        "BookingEvent",
        backref="user",
        cascade="all, delete-orphan",
    )

class Booking(Base, TimestampMixin):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("scheduled_date IS NOT NULL", name="ck_bookings_scheduled_date_required"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    service_type = Column(SAEnum(ServiceType, name="servicetype", native_enum=True), nullable=False)
    title = Column(String(255), nullable=True)
    details = Column(Text, nullable=False, default="")
    scheduled_date = Column(DateTime, nullable=False)

    status = Column(
        SAEnum(BookingStatus, name="bookingstatus", native_enum=True),
        nullable=False,
        default=BookingStatus.pending
    )

    user = relationship("User", back_populates="bookings")
    events = relationship(
        "BookingEvent",
        backref="booking",
        cascade="all, delete-orphan",
    )