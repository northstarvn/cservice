from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


def status_value(value):
    return getattr(value, "value", value)


def touch_booking(booking: models.Booking) -> None:
    booking.updated_at = datetime.now(timezone.utc)


def apply_booking_updates(booking: models.Booking, update_data: dict) -> None:
    for field, value in update_data.items():
        if not hasattr(booking, field):
            continue

        if field in ["service_type", "status"] and hasattr(value, "value"):
            setattr(booking, field, value.value)
        else:
            setattr(booking, field, value)


async def get_owned_booking(
    db: AsyncSession,
    booking_id: int,
    current_user: models.User,
) -> models.Booking:
    result = await db.execute(
        select(models.Booking).where(
            and_(models.Booking.id == booking_id, models.Booking.user_id == current_user.id)
        )
    )
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    return booking


def ensure_booking_transition_allowed(booking: models.Booking, target_status: models.BookingStatus) -> None:
    current_status = status_value(booking.status)
    target_value = status_value(target_status)

    if current_status == target_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Booking is already {target_value}",
        )

    if current_status == models.BookingStatus.cancelled.value and target_value == models.BookingStatus.confirmed.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cancelled bookings cannot be confirmed",
        )

    if current_status == models.BookingStatus.completed.value and target_value in {
        models.BookingStatus.pending.value,
        models.BookingStatus.cancelled.value,
    }:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Completed bookings cannot be moved back to an earlier state",
        )


async def create_booking_event(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    event_type: str,
    note: str = "",
    from_status=None,
    to_status=None,
):
    event = models.BookingEvent(
        booking_id=booking.id,
        user_id=current_user.id,
        event_type=event_type,
        from_status=status_value(from_status),
        to_status=status_value(to_status),
        note=note,
    )
    db.add(event)
    return event


async def transition_booking(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    target_status: models.BookingStatus,
    event_type: str,
    note: str,
):
    ensure_booking_transition_allowed(booking, target_status)
    previous_status = booking.status
    booking.status = target_status
    touch_booking(booking)
    event = await create_booking_event(
        db,
        booking,
        current_user,
        event_type=event_type,
        note=note,
        from_status=previous_status,
        to_status=booking.status,
    )
    await db.commit()
    await db.refresh(booking)
    await db.refresh(event)
    return {"booking": booking, "event": event}
