from datetime import datetime, timezone
import json
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.services.policy_scoring import can_access_functionality


def normalize_assignment_state(value: Optional[object]) -> str:
    if value is None:
        return "suggested"
    return getattr(value, "value", value)


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
    state = normalize_assignment_state(requested_assignment.get("state"))
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


def build_booking_assignment_report_from_record(
    assignment: models.BookingAssignment,
) -> dict:
    return {
        "generated_at": assignment.created_at,
        "booking_id": assignment.booking_id,
        "user_id": assignment.user_id,
        "current_state": normalize_assignment_state(assignment.state),
        "current_assignment_is_current": bool(getattr(assignment, "is_current", True)),
        "decisions": [
            {
                "booking_id": assignment.booking_id,
                "user_id": assignment.user_id,
                "room_id": assignment.room_id,
                "match_reason": assignment.match_reason,
                "state": normalize_assignment_state(assignment.state),
                "source": assignment.source,
                "explanation": assignment.explanation,
                "created_at": assignment.created_at,
                "is_current": bool(getattr(assignment, "is_current", True)),
            }
        ],
    }


def can_user_access_booking_functionality(policy_score: models.CustomerPolicyScore, functionality: str) -> bool:
    if functionality in {"create", "update", "delete", "history"}:
        return can_access_functionality(policy_score, required_tier="standard")
    if functionality in {"assign", "recommend", "match"}:
        return can_access_functionality(policy_score, required_tier="customer-premium")
    return can_access_functionality(policy_score, required_tier="restricted")


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


async def create_booking_assignment_event(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    assignment: models.BookingAssignment,
    event_type: str,
):
    note = {
        "assignment_id": assignment.id,
        "room_id": assignment.room_id,
        "state": normalize_assignment_state(assignment.state),
        "source": assignment.source,
        "explanation": assignment.explanation,
        "is_current": assignment.is_current,
    }
    return await create_booking_event(
        db,
        booking,
        current_user,
        event_type=event_type,
        note=note,
        from_status=booking.status,
        to_status=booking.status,
    )


async def create_booking_assignment(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    requested_assignment: Optional[dict] = None,
) -> models.BookingAssignment:
    requested_assignment = requested_assignment or {}
    assignment = models.BookingAssignment(
        booking_id=booking.id,
        user_id=current_user.id,
        room_id=requested_assignment.get("room_id", booking.id),
        match_reason=requested_assignment.get("match_reason", f"booking-{booking.id}-assignment"),
        state=normalize_assignment_state(requested_assignment.get("state")),
        source=requested_assignment.get("source", "booking-service"),
        explanation=requested_assignment.get("explanation", "Generated booking assignment."),
        is_current=True,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    await create_booking_assignment_event(
        db,
        booking,
        current_user,
        assignment,
        event_type="assignment_created",
    )
    await db.commit()
    return assignment


async def update_booking_assignment(
    db: AsyncSession,
    assignment: models.BookingAssignment,
    current_user: models.User,
    requested_assignment: Optional[dict] = None,
) -> models.BookingAssignment:
    requested_assignment = requested_assignment or {}
    if "room_id" in requested_assignment and requested_assignment["room_id"] is not None:
        assignment.room_id = requested_assignment["room_id"]
    if "match_reason" in requested_assignment and requested_assignment["match_reason"] is not None:
        assignment.match_reason = requested_assignment["match_reason"]
    if "state" in requested_assignment and requested_assignment["state"] is not None:
        assignment.state = normalize_assignment_state(requested_assignment["state"])
    if "source" in requested_assignment and requested_assignment["source"] is not None:
        assignment.source = requested_assignment["source"]
    if "explanation" in requested_assignment and requested_assignment["explanation"] is not None:
        assignment.explanation = requested_assignment["explanation"]
    assignment.is_current = True
    assignment.updated_at = datetime.now(timezone.utc)
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    await create_booking_assignment_event(
        db,
        assignment.booking,
        current_user,
        assignment,
        event_type="assignment_updated",
    )
    await db.commit()
    return assignment


async def get_latest_booking_assignment(
    db: AsyncSession,
    booking_id: int,
    user_id: int,
) -> Optional[models.BookingAssignment]:
    result = await db.execute(
        select(models.BookingAssignment)
        .where(models.BookingAssignment.booking_id == booking_id)
        .where(models.BookingAssignment.user_id == user_id)
        .where(models.BookingAssignment.is_current.is_(True))
        .order_by(models.BookingAssignment.created_at.desc(), models.BookingAssignment.id.desc())
    )
    assignment = result.scalars().first()
    if assignment:
        return assignment

    fallback_result = await db.execute(
        select(models.BookingAssignment)
        .where(models.BookingAssignment.booking_id == booking_id)
        .where(models.BookingAssignment.user_id == user_id)
        .order_by(models.BookingAssignment.created_at.desc(), models.BookingAssignment.id.desc())
    )
    return fallback_result.scalars().first()


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


async def ensure_booking_transition_allowed(booking: models.Booking, target_status) -> None:
    if status_value(booking.status) == status_value(target_status):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Booking already in target status")
    if status_value(booking.status) == "cancelled" and status_value(target_status) == "confirmed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cancelled bookings cannot be confirmed")


async def transition_booking(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    target_status,
    event_type: str,
    note: str = "",
) -> dict:
    await ensure_booking_transition_allowed(booking, target_status)
    control_posture = getattr(getattr(current_user, "policy_score", None), "control_posture", "observed")
    if control_posture in {"high_trust", "customer_trusted"}:
        note = note or "Booking transition completed under elevated posture"
    elif control_posture in {"constrained", "observed"}:
        note = note or "Booking transition completed under controlled posture"
    elif not note:
        note = "Booking transition completed"
    booking.status = target_status
    touch_booking(booking)
    event = await create_booking_event(
        db,
        booking,
        current_user,
        event_type=event_type,
        note=note,
        from_status=target_status,
        to_status=target_status,
    )
    db.add(booking)
    await db.commit()
    await db.refresh(booking)
    await db.refresh(event)
    return {"booking": booking, "event": event}


async def record_booking_mutation_event(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    event_type: str,
    previous_booking: Optional[models.Booking] = None,
    update_data: Optional[dict] = None,
    note: str = "",
) -> models.BookingEvent:
    note_payload = {
        "message": note,
        "booking_id": booking.id,
        "user_id": current_user.id,
        "update_fields": sorted((update_data or {}).keys()),
    }
    if previous_booking is not None:
        note_payload["previous_booking_id"] = previous_booking.id
        note_payload["previous_status"] = status_value(previous_booking.status)
    return await create_booking_event(
        db,
        booking,
        current_user,
        event_type=event_type,
        note=note_payload,
        from_status=booking.status,
        to_status=booking.status,
    )
