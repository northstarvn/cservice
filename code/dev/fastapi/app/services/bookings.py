from datetime import datetime, timezone
import json
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


def build_booking_assignment_decisions(
    booking: models.Booking,
    current_user: models.User,
    requested_assignment: Optional[dict] = None,
) -> List[dict]:
    created_at = datetime.now(timezone.utc)
    requested_assignment = requested_assignment or {}
    room_id = requested_assignment.get("room_id", booking.id)
    match_reason = requested_assignment.get(
        "match_reason",
        f"booking-{status_value(booking.status)}-and-user-{current_user.id}",
    )
    source = requested_assignment.get("source", "booking-service")
    explanation = requested_assignment.get(
        "explanation",
        "Deterministic booking assignment placeholder based on the current booking and user context.",
    )
    state = requested_assignment.get("state", "suggested")
    return [
        {
            "booking_id": booking.id,
            "user_id": current_user.id,
            "room_id": room_id,
            "match_reason": match_reason,
            "state": state,
            "source": source,
            "explanation": explanation,
            "created_at": created_at,
        }
    ]


def build_booking_assignment_report(
    booking: models.Booking,
    current_user: models.User,
    requested_assignment: Optional[dict] = None,
) -> dict:
    decisions = build_booking_assignment_decisions(booking, current_user, requested_assignment=requested_assignment)
    current_state = decisions[-1]["state"] if decisions else "suggested"
    return {
        "generated_at": datetime.now(timezone.utc),
        "booking_id": booking.id,
        "user_id": current_user.id,
        "current_state": current_state,
        "decisions": decisions,
    }


def build_booking_assignment_report_from_record(assignment: models.BookingAssignment) -> dict:
    created_at = assignment.created_at or datetime.now(timezone.utc)
    decision = {
        "booking_id": assignment.booking_id,
        "user_id": assignment.user_id,
        "room_id": assignment.room_id,
        "match_reason": assignment.match_reason,
        "state": assignment.state,
        "source": assignment.source,
        "explanation": assignment.explanation,
        "created_at": created_at,
    }
    return {
        "generated_at": created_at,
        "booking_id": assignment.booking_id,
        "user_id": assignment.user_id,
        "current_state": assignment.state,
        "decisions": [decision],
    }


async def persist_booking_assignment(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    requested_assignment: Optional[dict] = None,
) -> models.BookingAssignment:
    decision = build_booking_assignment_decisions(
        booking,
        current_user,
        requested_assignment=requested_assignment,
    )[0]
    assignment = models.BookingAssignment(
        booking_id=decision["booking_id"],
        user_id=decision["user_id"],
        room_id=decision["room_id"],
        match_reason=decision["match_reason"],
        state=decision["state"],
        source=decision["source"],
        explanation=decision["explanation"],
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    return assignment


async def update_booking_assignment(
    db: AsyncSession,
    assignment: models.BookingAssignment,
    requested_assignment: Optional[dict] = None,
) -> models.BookingAssignment:
    requested_assignment = requested_assignment or {}
    if "room_id" in requested_assignment and requested_assignment["room_id"] is not None:
        assignment.room_id = requested_assignment["room_id"]
    if "match_reason" in requested_assignment and requested_assignment["match_reason"] is not None:
        assignment.match_reason = requested_assignment["match_reason"]
    if "state" in requested_assignment and requested_assignment["state"] is not None:
        assignment.state = requested_assignment["state"]
    if "source" in requested_assignment and requested_assignment["source"] is not None:
        assignment.source = requested_assignment["source"]
    if "explanation" in requested_assignment and requested_assignment["explanation"] is not None:
        assignment.explanation = requested_assignment["explanation"]
    assignment.updated_at = datetime.now(timezone.utc)
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    return assignment


def status_value(value):
    return getattr(value, "value", value)


def touch_booking(booking: models.Booking) -> None:
    booking.updated_at = datetime.now(timezone.utc)


def build_booking_diff(before: models.Booking, update_data: dict) -> dict[str, dict[str, object]]:
    diff: dict[str, dict[str, object]] = {}
    for field, new_value in update_data.items():
        if not hasattr(before, field):
            continue
        old_value = getattr(before, field)
        old_normalized = status_value(old_value)
        new_normalized = status_value(new_value)
        if old_normalized == new_normalized:
            continue
        diff[field] = {
            "from": old_normalized,
            "to": new_normalized,
        }
    return diff


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
    note_value = note
    if isinstance(note, dict):
        note_value = json.dumps(note, default=str)
    event = models.BookingEvent(
        booking_id=booking.id,
        user_id=current_user.id,
        event_type=event_type,
        from_status=status_value(from_status),
        to_status=status_value(to_status),
        note=note_value,
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


async def record_booking_mutation_event(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    event_type: str,
    previous_booking: models.Booking,
    update_data: dict,
    note: str,
) -> models.BookingEvent:
    diff = build_booking_diff(previous_booking, update_data)
    return await create_booking_event(
        db,
        booking,
        current_user,
        event_type=event_type,
        note={
            "message": note,
            "diff": diff,
            "actor_id": current_user.id,
            "booking_id": booking.id,
            "mutated_fields": sorted(diff.keys()),
        },
        from_status=previous_booking.status,
        to_status=booking.status,
    )
