from dataclasses import dataclass
from datetime import datetime, timezone
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import models, security
from app.deps import get_current_admin_user
from app.services.bookings import apply_booking_updates, transition_booking
from app.services.bookings import create_booking_event, ensure_booking_transition_allowed
from app.routers.users import change_password
from app.schemas import schemas


@dataclass
class FakeUser:
    id: int = 1
    username: str = "tester"
    email: str = "tester@example.com"
    full_name: str | None = None
    hashed_password: str = "old-hash"
    is_admin: bool = False
    created_at: datetime = datetime.now(timezone.utc)
    updated_at: datetime = datetime.now(timezone.utc)


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0
        self.refreshed = []

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.commits += 1

    async def refresh(self, item):
        self.refreshed.append(item)


class FakeBooking:
    def __init__(self, booking_id: int = 10, status=models.BookingStatus.pending):
        self.id = booking_id
        self.status = status
        self.title = "Original title"
        self.details = "Original details"


def test_booking_event_relationship_metadata_is_explicit():
    assert models.Booking.events.property.backref is not None
    assert models.Booking.user.property is not None


def test_chat_history_relationship_metadata_is_explicit():
    assert models.User.chat_history.property.backref is not None
    assert models.User.chat_history.property.passive_deletes is True


class FakeUpdatePayload:
    def model_dump(self, exclude_unset=True, exclude_none=True):
        return {"title": "Updated title", "status": models.BookingStatus.confirmed}


@pytest.mark.asyncio
async def test_get_current_admin_user_rejects_non_admin():
    with pytest.raises(Exception) as exc_info:
        await get_current_admin_user(current_user=FakeUser(is_admin=False))

    assert getattr(exc_info.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_get_current_admin_user_accepts_admin():
    admin_user = FakeUser(is_admin=True)

    result = await get_current_admin_user(current_user=admin_user)

    assert result is admin_user


@pytest.mark.asyncio
async def test_change_password_updates_hash():
    db = FakeSession()
    user = FakeUser(hashed_password=security.get_password_hash("old-password"))
    payload = schemas.PasswordChange(current_password="old-password", new_password="new-password")

    result = await change_password(payload=payload, current_user=user, db=db)

    assert result == {"message": "Password updated successfully"}
    assert user.hashed_password != security.get_password_hash("old-password")
    assert db.commits == 1


@pytest.mark.asyncio
async def test_create_booking_event_captures_transition_fields():
    db = FakeSession()
    booking = FakeBooking()
    user = FakeUser()

    event = await create_booking_event(
        db,
        booking=booking,
        current_user=user,
        event_type="confirmed",
        note="confirmed by test",
        from_status=models.BookingStatus.pending,
        to_status=models.BookingStatus.confirmed,
    )

    assert event in db.added
    assert event.event_type == "confirmed"
    assert event.from_status == "pending"
    assert event.to_status == "confirmed"
    assert event.note == "confirmed by test"


@pytest.mark.asyncio
async def test_confirming_cancelled_booking_is_rejected():
    booking = FakeBooking(status=models.BookingStatus.cancelled)

    with pytest.raises(Exception) as exc_info:
        ensure_booking_transition_allowed(booking, models.BookingStatus.confirmed)

    assert getattr(exc_info.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_reapplying_same_booking_state_is_rejected():
    booking = FakeBooking(status=models.BookingStatus.pending)

    with pytest.raises(Exception) as exc_info:
        ensure_booking_transition_allowed(booking, models.BookingStatus.pending)

    assert getattr(exc_info.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_apply_booking_updates_handles_enum_and_plain_fields():
    booking = FakeBooking()

    apply_booking_updates(
        booking,
        {"title": "Updated title", "status": models.BookingStatus.cancelled, "unknown": "ignored"},
    )

    assert booking.title == "Updated title"
    assert booking.status == models.BookingStatus.cancelled.value
    assert not hasattr(booking, "unknown")


@pytest.mark.asyncio
async def test_transition_booking_creates_event_and_updates_booking():
    db = FakeSession()
    booking = FakeBooking()
    user = FakeUser()

    result = await transition_booking(
        db,
        booking=booking,
        current_user=user,
        target_status=models.BookingStatus.confirmed,
        event_type="confirmed",
        note="Booking confirmed",
    )

    assert db.commits == 1
    assert len(db.refreshed) == 2
    assert result["booking"].status == models.BookingStatus.confirmed
    assert result["event"].event_type == "confirmed"
