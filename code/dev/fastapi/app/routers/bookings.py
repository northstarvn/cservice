from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_, desc, func
from sqlalchemy.exc import IntegrityError
from typing import Optional
from datetime import datetime, timezone, timedelta
from app import models, deps
from app.schemas import schemas
from app.services.bookings import (
    apply_booking_updates,
    build_booking_assignment_history_summary,
    build_booking_assignment_report,
    build_booking_assignment_report_from_record,
    build_booking_assignment_report_from_record_compat,
    build_booking_assignment_report_payload,
    build_booking_assignment_report_page,
    booking_to_event_out,
    build_typed_booking_assignment_history_summary,
    build_typed_booking_assignment_report,
    build_typed_booking_assignment_report_from_existing_report,
    build_typed_booking_assignment_report_from_payload,
    build_typed_booking_assignment_report_from_record,
    build_typed_booking_assignment_reports_from_records,
    build_typed_booking_assignment_summary,
    build_typed_booking_operation_report,
    can_user_access_booking_functionality,
    count_booking_statuses,
    count_status_values,
    create_booking_assignment,
    create_booking_event,
    get_booking_assignment_by_id,
    get_latest_booking_assignment,
    get_owned_booking,
    list_booking_assignments,
    record_booking_mutation_event,
    touch_booking,
    transition_booking,
    update_booking_assignment,
)

router = APIRouter()


_current_control_posture = deps.current_control_posture


def _booking_response_guidance(control_posture: str) -> str:
    if control_posture in {"high_trust", "customer_trusted"}:
        return "Booking recorded with expanded control posture."
    if control_posture in {"constrained", "observed"}:
        return "Booking recorded with controlled access posture."
    return "Booking recorded successfully."


def _controlled_booking_details(details: str, control_posture: str, action: str) -> str:
    note = "Controlled posture note: booking {action} under monitored access.".format(action=action)
    guidance = _booking_response_guidance(control_posture)
    parts = [details] if details else []
    if control_posture in {"constrained", "observed"}:
        parts.append(note)
    parts.append(guidance)
    return "\n\n".join(parts)


def _event_contains_any_field(note: str, fields: list[str]) -> bool:
    normalized_note = (note or "").lower()
    return any(field in normalized_note for field in fields)


def _build_booking_assignment_report(
    booking: models.Booking,
    current_user: models.User,
    report: dict,
) -> schemas.BookingAssignmentReport:
    return build_typed_booking_assignment_report(
        booking,
        current_user,
        report,
        _current_control_posture(current_user),
    )


def _recent_mutation_fields_from_events(events, fields=("title", "details", "scheduled_date", "service_type", "status")) -> list[str]:
    return sorted(
        {
            field
            for event in events[:5]
            for field in fields
            if _event_contains_any_field(getattr(event, "note", "") or "", [field])
        }
    )


def _event_type_counts(events) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(getattr(event, "event_type", "") or "unknown").lower()
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _build_booking_audit_summary(
    booking: models.Booking,
    events,
    current_user: models.User,
    recent_mutation_fields: list[str],
) -> schemas.BookingAuditSummary:
    latest_event = events[0] if events else None
    return schemas.BookingAuditSummary(
        booking_id=booking.id,
        user_id=current_user.id,
        total_events=len(events),
        assignment_events=sum(1 for event in events if str(getattr(event, "event_type", "")).startswith("assignment_")),
        created_events=sum(1 for event in events if event.event_type == "created"),
        status_updates=sum(1 for event in events if event.event_type == "status_updated"),
        event_type_counts=_event_type_counts(events),
        latest_event_at=latest_event.created_at if latest_event else None,
        latest_event_note=latest_event.note if latest_event else None,
        recent_mutation_fields=recent_mutation_fields,
        control_posture=_current_control_posture(current_user),
    )


@router.post("/", response_model=schemas.BookingOut)
async def create_booking(
    booking: schemas.BookingCreate,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db)
):
    if not can_user_access_booking_functionality(policy_score, "create"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking creation requires a higher policy tier")
    try:
        db_booking = models.Booking(
            user_id=current_user.id,
            service_type=booking.service_type,
            title=booking.title,
            details=booking.details or "",
            scheduled_date=booking.scheduled_date,
            status=models.BookingStatus.pending
        )
        db.add(db_booking)
        await create_booking_event(
            db,
            db_booking,
            current_user,
            event_type="created",
            note="Booking created",
            to_status=models.BookingStatus.pending,
        )
        await db.commit()
        await db.refresh(db_booking)
        control_posture = _current_control_posture(current_user)
        db_booking.control_posture = control_posture
        db_booking.details = _controlled_booking_details(db_booking.details, control_posture, "created")
        return db_booking
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create booking: {str(e)}"
        )

@router.get("/", response_model=schemas.PaginatedBookings)
async def get_bookings(
    page: int = 1,
    per_page: int = 10,
    booking_status: Optional[schemas.BookingStatus] = None,
    service_type: Optional[schemas.ServiceType] = None,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db)
):
    if not can_user_access_booking_functionality(policy_score, "history"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking history requires a higher policy tier")
    control_posture = _current_control_posture(current_user)
    query = select(models.Booking).where(models.Booking.user_id == current_user.id)
    if control_posture in {"constrained", "observed"}:
        query = query.where(models.Booking.status.in_([models.BookingStatus.pending, models.BookingStatus.confirmed]))
    if booking_status:
        query = query.where(models.Booking.status == booking_status.value)
    if service_type:
        query = query.where(models.Booking.service_type == service_type.value)

    count_query = select(func.count(models.Booking.id)).where(models.Booking.user_id == current_user.id)
    if control_posture in {"constrained", "observed"}:
        count_query = count_query.where(models.Booking.status.in_([models.BookingStatus.pending, models.BookingStatus.confirmed]))
    if booking_status:
        count_query = count_query.where(models.Booking.status == booking_status.value)
    if service_type:
        count_query = count_query.where(models.Booking.service_type == service_type.value)

    total = (await db.execute(count_query)).scalar() or 0
    offset = (page - 1) * per_page

    result = await db.execute(
        query.order_by(desc(models.Booking.created_at)).offset(offset).limit(per_page)
    )
    bookings = result.scalars().all()

    pages = (total + per_page - 1) // per_page if total else 0
    return schemas.PaginatedBookings(
        items=bookings,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages
    )

@router.get("/{booking_id}", response_model=schemas.BookingOut)
async def get_booking(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db)
):
    if not can_user_access_booking_functionality(policy_score, "history"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking access requires a higher policy tier")
    result = await db.execute(
        select(models.Booking).where(
            and_(models.Booking.id == booking_id, models.Booking.user_id == current_user.id)
        )
    )
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    control_posture = _current_control_posture(current_user)
    booking.control_posture = control_posture
    if control_posture in {"constrained", "observed"} and booking.details:
        booking.details = f"{booking.details}\n\nView limited by control posture."
    return booking

@router.put("/{booking_id}", response_model=schemas.BookingOut)
async def update_booking(
    booking_id: int,
    booking_update: schemas.BookingUpdate,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db)
):
    if not can_user_access_booking_functionality(policy_score, "update"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking updates require a higher policy tier")
    try:
        result = await db.execute(
            select(models.Booking).where(
                and_(models.Booking.id == booking_id, models.Booking.user_id == current_user.id)
            )
        )
        booking = result.scalar_one_or_none()
        if not booking:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")

        update_data = booking_update.model_dump(exclude_unset=True, exclude_none=True)
        previous_booking = models.Booking(
            id=booking.id,
            user_id=booking.user_id,
            service_type=booking.service_type,
            title=booking.title,
            details=booking.details,
            scheduled_date=booking.scheduled_date,
            status=booking.status,
        )
        old_status = booking.status
        history_fields = {"title", "details", "scheduled_date", "service_type"}
        history_changed = any(field in update_data and getattr(booking, field) != update_data[field] for field in history_fields)
        apply_booking_updates(booking, update_data)
        touch_booking(booking)

        if history_changed:
            await record_booking_mutation_event(
                db,
                booking,
                current_user,
                event_type="updated",
                previous_booking=previous_booking,
                update_data=update_data,
                note="Booking details updated",
            )

        if "status" in update_data and booking.status != old_status:
            await create_booking_event(
                db,
                booking,
                current_user,
                event_type="status_updated",
                note="Booking status updated",
                from_status=old_status,
                to_status=booking.status,
            )

        await db.commit()
        await db.refresh(booking)
        control_posture = _current_control_posture(current_user)
        booking.control_posture = control_posture
        booking.details = _controlled_booking_details(booking.details, control_posture, "updated")
        return booking

    except ValueError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid data: {str(e)}"
        )
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database constraint violation: {str(e)}"
        )
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update booking: {str(e)}"
        )

@router.delete("/{booking_id}")
async def delete_booking(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db)
):
    if not can_user_access_booking_functionality(policy_score, "delete"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking deletion requires a higher policy tier")
    result = await db.execute(
        select(models.Booking).where(
            and_(models.Booking.id == booking_id, models.Booking.user_id == current_user.id)
        )
    )
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")

    await create_booking_event(
        db,
        booking,
        current_user,
        event_type="deleted",
        note={
            "message": "Booking deleted",
            "diff": {
                "status": {
                    "from": booking.status,
                    "to": booking.status,
                }
            },
            "actor_id": current_user.id,
            "booking_id": booking.id,
        },
        from_status=booking.status,
        to_status=booking.status,
    )
    await db.delete(booking)
    await db.commit()
    control_posture = _current_control_posture(current_user)
    if control_posture in {"constrained", "observed"}:
        message = "Booking deleted successfully after controlled access verification. Control posture note recorded."
    else:
        message = "Booking deleted successfully."
    return {"message": message, "control_posture": control_posture}


@router.get("/{booking_id}/history", response_model=schemas.BookingHistoryReport)
async def get_booking_history(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "history"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking history requires a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)

    event_result = await db.execute(
        select(models.BookingEvent)
        .where(models.BookingEvent.booking_id == booking_id)
        .where(models.BookingEvent.user_id == current_user.id)
        .order_by(desc(models.BookingEvent.created_at), desc(models.BookingEvent.id))
    )
    events = event_result.scalars().all()
    control_posture = _current_control_posture(current_user)
    visible_events = events if control_posture not in {"constrained", "observed"} else [
        event for event in events if not event.event_type.startswith("assignment_")
    ]
    history_summary = build_booking_assignment_history_summary(
        [event for event in events if event.event_type.startswith("assignment_")]
    )

    return schemas.BookingHistoryReport(
        booking_id=booking.id,
        user_id=current_user.id,
        current_status=booking.status,
        event_count=len(visible_events),
        assignment_events=history_summary["assignment_events"],
        event_type_counts=history_summary["event_type_counts"],
        control_posture=control_posture,
        items=[
            booking_to_event_out(event)
            for event in visible_events
        ],
    )


@router.get("/{booking_id}/audit-summary", response_model=schemas.BookingAuditSummary)
async def get_booking_audit_summary_controlled(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "history"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking audit access requires a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)

    event_result = await db.execute(
        select(models.BookingEvent)
        .where(models.BookingEvent.booking_id == booking_id)
        .where(models.BookingEvent.user_id == current_user.id)
        .order_by(desc(models.BookingEvent.created_at), desc(models.BookingEvent.id))
    )
    events = event_result.scalars().all()
    control_posture = _current_control_posture(current_user)
    recent_mutation_fields = (
        ["status", "assignment", "note"]
        if control_posture in {"constrained", "observed"}
        else _recent_mutation_fields_from_events(events)
    )
    return _build_booking_audit_summary(booking, events, current_user, recent_mutation_fields)


@router.get("/{booking_id}/operation-report", response_model=schemas.BookingOperationReport)
async def get_booking_operation_report(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "history"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking reporting requires a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)
    assignments = await list_booking_assignments(db, booking_id, current_user.id)
    event_result = await db.execute(
        select(models.BookingEvent)
        .where(models.BookingEvent.booking_id == booking_id)
        .where(models.BookingEvent.user_id == current_user.id)
        .order_by(desc(models.BookingEvent.created_at), desc(models.BookingEvent.id))
    )
    events = event_result.scalars().all()
    control_posture = _current_control_posture(current_user)
    return build_typed_booking_operation_report(booking, assignments, events, control_posture)


@router.get("/{booking_id}/assignment/history", response_model=schemas.BookingAssignmentHistoryReport)
async def get_booking_assignment_history(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "history"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking assignment history requires a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)

    event_result = await db.execute(
        select(models.BookingEvent)
        .where(models.BookingEvent.booking_id == booking_id)
        .where(models.BookingEvent.user_id == current_user.id)
        .where(models.BookingEvent.event_type.like("assignment_%"))
        .order_by(desc(models.BookingEvent.created_at), desc(models.BookingEvent.id))
    )
    events = event_result.scalars().all()
    control_posture = _current_control_posture(current_user)
    visible_events = events if control_posture not in {"constrained", "observed"} else []
    history_summary = build_booking_assignment_history_summary(events)

    return schemas.BookingAssignmentHistoryReport(
        booking_id=booking.id,
        user_id=current_user.id,
        event_count=len(visible_events),
        assignment_events=history_summary["assignment_events"],
        event_type_counts=history_summary["event_type_counts"],
        control_posture=control_posture,
        items=[
            booking_to_event_out(event, is_assignment_event=True)
            for event in visible_events
        ],
    )


@router.get("/{booking_id}/assignment/history/summary", response_model=schemas.BookingAssignmentHistorySummary)
async def get_booking_assignment_history_summary(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "history"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking assignment history requires a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)
    event_result = await db.execute(
        select(models.BookingEvent)
        .where(models.BookingEvent.booking_id == booking_id)
        .where(models.BookingEvent.user_id == current_user.id)
        .where(models.BookingEvent.event_type.like("assignment_%"))
        .order_by(models.BookingEvent.created_at.desc())
    )
    events = event_result.scalars().all()

    return build_typed_booking_assignment_history_summary(
        booking,
        current_user,
        events,
        _current_control_posture(current_user),
    )


@router.get("/{booking_id}/assignment", response_model=schemas.BookingAssignmentReport)
async def get_booking_assignment_report(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "recommend"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking assignment access requires a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)
    assignment = await get_latest_booking_assignment(db, booking.id, current_user.id)
    control_posture = _current_control_posture(current_user)
    if assignment:
        return build_typed_booking_assignment_report_from_record(
            assignment,
            current_user,
            control_posture,
        )

    return build_typed_booking_assignment_report_from_payload(
        booking,
        current_user,
        build_booking_assignment_report_payload(booking, current_user),
        control_posture,
    )


@router.post("/{booking_id}/assignment", response_model=schemas.BookingAssignmentReport)
async def create_booking_assignment_report(
    booking_id: int,
    assignment_request: schemas.BookingAssignmentRequest,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "assign"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking assignment creation requires a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)
    assignment = await create_booking_assignment(
        db,
        booking,
        current_user,
        requested_assignment=assignment_request.model_dump(exclude_none=True),
    )
    return build_typed_booking_assignment_report_from_record(
        assignment,
        current_user,
        _current_control_posture(current_user),
    )


@router.patch("/{booking_id}/assignment", response_model=schemas.BookingAssignmentReport)
async def update_booking_assignment_report(
    booking_id: int,
    assignment_update: schemas.BookingAssignmentUpdate,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "assign"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking assignment updates require a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)
    assignment = await get_latest_booking_assignment(db, booking.id, current_user.id)
    requested_assignment = assignment_update.model_dump(exclude_none=True)

    if assignment:
        updated_assignment = await update_booking_assignment(
            db,
            assignment,
            current_user,
            requested_assignment=requested_assignment,
        )
        return _build_booking_assignment_report(
            booking,
            current_user,
            build_booking_assignment_report_from_record(updated_assignment),
        )

    created_assignment = await create_booking_assignment(
        db,
        booking,
        current_user,
        requested_assignment=requested_assignment,
    )
    return _build_booking_assignment_report(
        booking,
        current_user,
        build_booking_assignment_report_from_record_compat(created_assignment),
    )


@router.get("/{booking_id}/assignment/current", response_model=schemas.BookingAssignmentReport)
async def get_current_booking_assignment_report(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "recommend"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking assignment access requires a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)
    assignment = await get_latest_booking_assignment(db, booking.id, current_user.id)
    if assignment:
        return _build_booking_assignment_report(
            booking,
            current_user,
            build_booking_assignment_report_from_record(assignment),
        )

    return build_typed_booking_assignment_report(
        booking,
        current_user,
        build_booking_assignment_report(booking, current_user),
        _current_control_posture(current_user),
    )


@router.get("/{booking_id}/assignment/reports", response_model=schemas.BookingAssignmentReportPage)
async def get_booking_assignment_reports(
    booking_id: int,
    page: int = 1,
    per_page: int = 50,
    sort_order: str = "desc",
    state: Optional[str] = None,
    source: Optional[str] = None,
    room_id: Optional[int] = None,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "history"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking assignment history requires a higher policy tier")
    if page < 1:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="page must be at least 1")
    if per_page < 1:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="per_page must be at least 1")
    if sort_order not in {"asc", "desc"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="sort_order must be 'asc' or 'desc'")
    booking = await get_owned_booking(db, booking_id, current_user)
    assignments = await list_booking_assignments(db, booking.id, current_user.id)
    if sort_order == "asc":
        assignments = list(reversed(assignments))
    if state is not None:
        normalized_state = state.lower()
        if normalized_state not in {"suggested", "active", "confirmed", "declined", "cancelled"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="state must be one of suggested, active, confirmed, declined, or cancelled")
        assignments = [assignment for assignment in assignments if str(assignment.state).lower() == normalized_state]
    if source is not None:
        normalized_source = source.strip().lower()
        if not normalized_source:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="source must not be empty")
        assignments = [assignment for assignment in assignments if str(assignment.source).lower() == normalized_source]
    if room_id is not None:
        assignments = [assignment for assignment in assignments if assignment.room_id == room_id]
    if created_after is not None:
        assignments = [assignment for assignment in assignments if getattr(assignment, "created_at", None) and assignment.created_at >= created_after]
    if created_before is not None:
        assignments = [assignment for assignment in assignments if getattr(assignment, "created_at", None) and assignment.created_at <= created_before]
    page_summary = build_booking_assignment_report_page(assignments)
    total_assignments = page_summary.total_assignments
    pages = (total_assignments + per_page - 1) // per_page if total_assignments else 0
    offset = (page - 1) * per_page
    assignments = assignments[offset:offset + per_page]
    control_posture = _current_control_posture(current_user)

    items = build_typed_booking_assignment_reports_from_records(assignments, current_user, control_posture)

    return schemas.BookingAssignmentReportPage(
        booking_id=booking.id,
        user_id=current_user.id,
        total_assignments=total_assignments,
        current_assignments=page_summary.current_assignments,
        historical_assignments=page_summary.historical_assignments,
        state_counts=page_summary.state_counts,
        source_counts=page_summary.source_counts,
        created_after=created_after,
        created_before=created_before,
        page=page,
        per_page=per_page,
        pages=pages,
        items=items,
        control_posture=control_posture,
    )


@router.get("/{booking_id}/assignment/summary", response_model=schemas.BookingAssignmentSummary)
async def get_booking_assignment_summary(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "history"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking assignment history requires a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)
    assignments = await list_booking_assignments(db, booking.id, current_user.id)

    return build_typed_booking_assignment_summary(
        booking,
        current_user,
        assignments,
        _current_control_posture(current_user),
    )


# NOTE: registered before "/{booking_id}/assignment/{assignment_id}" so the fixed
# segment is not captured as an int path parameter.
@router.get("/{booking_id}/assignment/{assignment_id}", response_model=schemas.BookingAssignmentReport)
async def get_booking_assignment_by_id_report(
    booking_id: int,
    assignment_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    if not can_user_access_booking_functionality(policy_score, "recommend"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booking assignment access requires a higher policy tier")
    booking = await get_owned_booking(db, booking_id, current_user)
    assignment = await get_booking_assignment_by_id(db, booking.id, assignment_id, current_user.id)
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking assignment not found")

    return _build_booking_assignment_report(
        booking,
        current_user,
        build_booking_assignment_report_from_record(assignment),
    )


@router.get("/{booking_id}/audit", response_model=schemas.BookingAuditSummary)
async def get_booking_audit_summary(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    booking = await get_owned_booking(db, booking_id, current_user)

    event_result = await db.execute(
        select(models.BookingEvent)
        .where(models.BookingEvent.booking_id == booking_id)
        .where(models.BookingEvent.user_id == current_user.id)
        .order_by(desc(models.BookingEvent.created_at))
    )
    events = event_result.scalars().all()

    return _build_booking_audit_summary(
        booking,
        events,
        current_user,
        _recent_mutation_fields_from_events(events),
    )


@router.get("/analytics/summary", response_model=schemas.AdminAnalyticsSummary)
async def get_booking_analytics_summary(
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    total_users_result = await db.execute(select(func.count(models.User.id)))
    total_bookings_result = await db.execute(select(func.count(models.Booking.id)))
    total_events_result = await db.execute(select(func.count(models.BookingEvent.id)))

    status_rows = await db.execute(
        select(models.Booking.status, func.count(models.Booking.id)).group_by(models.Booking.status)
    )
    event_rows = await db.execute(
        select(models.BookingEvent.event_type, func.count(models.BookingEvent.id)).group_by(models.BookingEvent.event_type)
    )

    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=7)
    recent_bookings_result = await db.execute(
        select(func.count(models.Booking.id)).where(models.Booking.created_at >= recent_cutoff)
    )
    recent_events_result = await db.execute(
        select(func.count(models.BookingEvent.id)).where(models.BookingEvent.created_at >= recent_cutoff)
    )

    event_rows_all = event_rows.all()
    bookings_by_status = count_booking_statuses(status_rows.all())
    event_counts = [
        schemas.AnalyticsEventCount(event_type=str(event_type), count=int(count or 0))
        for event_type, count in event_rows_all
    ]
    assignment_events_total = sum(
        count for event_type, count in event_rows_all if str(event_type).startswith("assignment_")
    )
    booking_events_total = int(total_events_result.scalar() or 0)
    assignment_event_ratio = (
        float(assignment_events_total) / float(booking_events_total)
        if booking_events_total
        else 0.0
    )

    return schemas.AdminAnalyticsSummary(
        generated_at=now,
        total_users=int(total_users_result.scalar() or 0),
        total_bookings=int(total_bookings_result.scalar() or 0),
        bookings_by_status=bookings_by_status,
        booking_events_total=booking_events_total,
        assignment_events_total=int(assignment_events_total or 0),
        assignment_event_ratio=assignment_event_ratio,
        booking_events_by_type=event_counts,
        recent_bookings=int(recent_bookings_result.scalar() or 0),
        recent_events=int(recent_events_result.scalar() or 0),
        control_posture=_current_control_posture(current_user),
    )


@router.get("/analytics/events", response_model=schemas.BookingEventFilterSummary)
async def get_booking_event_summary(
    event_type: Optional[str] = None,
    booking_id: Optional[int] = None,
    user_id: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    page: int = 1,
    per_page: int = 50,
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    query = select(models.BookingEvent)
    count_query = select(func.count(models.BookingEvent.id))
    filters = []
    if event_type:
        filters.append(models.BookingEvent.event_type == event_type)
    if booking_id is not None:
        filters.append(models.BookingEvent.booking_id == booking_id)
    if user_id is not None:
        filters.append(models.BookingEvent.user_id == user_id)
    if start_date is not None:
        filters.append(models.BookingEvent.created_at >= start_date)
    if end_date is not None:
        filters.append(models.BookingEvent.created_at <= end_date)
    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total_events = int((await db.execute(count_query)).scalar() or 0)
    pages = (total_events + per_page - 1) // per_page if total_events else 0
    offset = (page - 1) * per_page

    query = query.order_by(desc(models.BookingEvent.created_at)).offset(offset).limit(per_page)
    result = await db.execute(query)
    events = result.scalars().all()

    status_counts = count_status_values(event.to_status for event in events)
    assignment_events = sum(1 for event in events if event.event_type.startswith("assignment_"))
    assignment_event_ratio = (assignment_events / len(events)) if events else 0.0

    return schemas.BookingEventFilterSummary(
        generated_at=datetime.now(timezone.utc),
        total_events=total_events,
        page=page,
        per_page=per_page,
        pages=pages,
        event_type=event_type,
        booking_id=booking_id,
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
        statuses=status_counts,
        events=events,
        assignment_events=assignment_events,
        assignment_event_ratio=assignment_event_ratio,
    )


@router.get("/analytics/export", response_model=schemas.BookingExportReport)
async def export_booking_data(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    booking_query = select(models.Booking)
    event_query = select(models.BookingEvent)
    booking_filters = []
    event_filters = []

    if start_date is not None:
        booking_filters.append(models.Booking.created_at >= start_date)
        event_filters.append(models.BookingEvent.created_at >= start_date)
    if end_date is not None:
        booking_filters.append(models.Booking.created_at <= end_date)
        event_filters.append(models.BookingEvent.created_at <= end_date)

    if booking_filters:
        booking_query = booking_query.where(and_(*booking_filters))
    if event_filters:
        event_query = event_query.where(and_(*event_filters))

    booking_query = booking_query.order_by(desc(models.Booking.created_at))
    event_query = event_query.order_by(desc(models.BookingEvent.created_at))

    bookings = (await db.execute(booking_query)).scalars().all()
    events = (await db.execute(event_query)).scalars().all()

    assignment_events_total = sum(1 for event in events if event.event_type.startswith("assignment_"))
    control_posture = _current_control_posture(current_user)
    if control_posture in {"constrained", "observed"}:
        events = [event for event in events if not event.event_type.startswith("assignment_")]
        assignment_events_total = 0
    assignment_event_ratio = (assignment_events_total / len(events)) if events else 0.0

    return schemas.BookingExportReport(
        generated_at=datetime.now(timezone.utc),
        total_bookings=len(bookings),
        total_events=len(events),
        assignment_events_total=assignment_events_total,
        assignment_event_ratio=assignment_event_ratio,
        bookings=bookings,
        events=events,
        control_posture=control_posture,
    )


@router.post("/{booking_id}/confirm", response_model=schemas.BookingTransitionResult)
async def confirm_booking(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    booking = await get_owned_booking(db, booking_id, current_user)
    return await transition_booking(
        db,
        booking,
        current_user,
        models.BookingStatus.confirmed,
        event_type="confirmed",
        note="Booking confirmed",
    )


@router.post("/{booking_id}/cancel", response_model=schemas.BookingTransitionResult)
async def cancel_booking(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    booking = await get_owned_booking(db, booking_id, current_user)
    return await transition_booking(
        db,
        booking,
        current_user,
        models.BookingStatus.cancelled,
        event_type="cancelled",
        note="Booking cancelled",
    )


@router.post("/{booking_id}/reopen", response_model=schemas.BookingTransitionResult)
async def reopen_booking(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    booking = await get_owned_booking(db, booking_id, current_user)
    return await transition_booking(
        db,
        booking,
        current_user,
        models.BookingStatus.pending,
        event_type="reopened",
        note="Booking reopened",
    )
