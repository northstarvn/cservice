from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_, desc, func
from sqlalchemy.exc import IntegrityError
from typing import Optional
from datetime import datetime, timezone, timedelta
from sqlalchemy.inspection import inspect
from app import models, deps
from app.schemas import schemas
from app.services.bookings import (
    apply_booking_updates,
    build_booking_diff,
    build_booking_assignment_report,
    build_booking_assignment_report_from_record,
    create_booking_event,
    ensure_booking_transition_allowed,
    get_owned_booking,
    can_user_access_booking_functionality,
    get_latest_booking_assignment,
    create_booking_assignment,
    touch_booking,
    record_booking_mutation_event,
    transition_booking,
)

router = APIRouter()


def _current_control_posture(current_user: models.User) -> str:
    policy_score = getattr(current_user, "policy_score", None)
    return getattr(policy_score, "control_posture", "observed") if policy_score else "observed"


def _booking_response_guidance(control_posture: str) -> str:
    if control_posture in {"high_trust", "customer_trusted"}:
        return "Booking recorded with expanded control posture."
    if control_posture in {"constrained", "observed"}:
        return "Booking recorded with controlled access posture."
    return "Booking recorded successfully."

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
        if control_posture in {"constrained", "observed"} and db_booking.details:
            db_booking.details = f"{db_booking.details}\n\nControlled posture note: booking created under monitored access."
        if db_booking.details:
            db_booking.details = f"{db_booking.details}\n\n{_booking_response_guidance(control_posture)}"
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
        booking_diff = build_booking_diff(previous_booking, update_data)
        
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
        if control_posture in {"constrained", "observed"} and booking.details:
            booking.details = f"{booking.details}\n\nControlled posture note: booking updated under monitored access."
        if booking.details and control_posture in {"constrained", "observed"}:
            booking.details = f"{booking.details}\n\n{_booking_response_guidance(control_posture)}"
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
    if control_posture in {"high_trust", "customer_trusted"}:
        message = "Booking deleted successfully."
    elif control_posture in {"constrained", "observed"}:
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
    booking_result = await db.execute(
        select(models.Booking).where(
            and_(models.Booking.id == booking_id, models.Booking.user_id == current_user.id)
        )
    )
    booking = booking_result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")

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

    return schemas.BookingHistoryReport(
        booking_id=booking.id,
        user_id=current_user.id,
        current_status=booking.status,
        event_count=len(visible_events),
        control_posture=control_posture,
        items=[
            schemas.BookingEventOut(
                **inspect(event).dict,
                is_assignment_event=event.event_type.startswith("assignment_"),
            )
            for event in visible_events
        ],
    )


@router.get("/{booking_id}/audit-summary", response_model=schemas.BookingAuditSummary)
async def get_booking_audit_summary(
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
    assignment_events = [event for event in events if event.event_type.startswith("assignment_")]
    created_events = [event for event in events if event.event_type == "created"]
    status_updates = [event for event in events if event.event_type == "status_updated"]
    latest_event = events[0] if events else None
    control_posture = _current_control_posture(current_user)
    recent_mutation_fields = []
    if control_posture in {"constrained", "observed"}:
        recent_mutation_fields = ["status", "assignment", "note"]

    return schemas.BookingAuditSummary(
        booking_id=booking.id,
        user_id=current_user.id,
        total_events=len(events),
        assignment_events=len(assignment_events),
        created_events=len(created_events),
        status_updates=len(status_updates),
        latest_event_at=latest_event.created_at if latest_event else None,
        latest_event_note=latest_event.note if latest_event else None,
        recent_mutation_fields=recent_mutation_fields,
        control_posture=control_posture,
    )


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

    return schemas.BookingAssignmentHistoryReport(
        booking_id=booking.id,
        user_id=current_user.id,
        event_count=len(visible_events),
        control_posture=control_posture,
        items=[
            schemas.BookingEventOut(
                **inspect(event).dict,
                is_assignment_event=True,
            )
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

    summary_result = await db.execute(
        select(
            func.count(models.BookingEvent.id).label("event_count"),
            func.max(models.BookingEvent.created_at).label("latest_event_at"),
            func.max(models.BookingEvent.event_type).label("latest_event_type"),
        )
        .where(models.BookingEvent.booking_id == booking_id)
        .where(models.BookingEvent.user_id == current_user.id)
        .where(models.BookingEvent.event_type.like("assignment_%"))
    )
    summary_row = summary_result.one()

    return schemas.BookingAssignmentHistorySummary(
        booking_id=booking.id,
        user_id=current_user.id,
        event_count=summary_row.event_count or 0,
        latest_event_at=summary_row.latest_event_at,
        latest_event_type=summary_row.latest_event_type,
        control_posture=_current_control_posture(current_user),
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
    report = (
        build_booking_assignment_report_from_record(assignment)
        if assignment
        else build_booking_assignment_report(booking, current_user)
    )
    control_posture = _current_control_posture(current_user)
    decisions = [schemas.BookingAssignmentDecision(**decision) for decision in report["decisions"]]
    if control_posture in {"constrained", "observed"}:
        for decision in decisions:
            decision.explanation = decision.explanation or "Controlled posture note: assignment recommendation is monitored."
    return schemas.BookingAssignmentReport(
        generated_at=report["generated_at"],
        booking_id=report["booking_id"],
        user_id=report["user_id"],
        current_state=report["current_state"],
        current_assignment_is_current=report["current_assignment_is_current"],
        decisions=decisions,
        control_posture=control_posture,
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
    report = build_booking_assignment_report_from_record(assignment)
    control_posture = _current_control_posture(current_user)
    decisions = [schemas.BookingAssignmentDecision(**decision) for decision in report["decisions"]]
    if control_posture in {"constrained", "observed"}:
        for decision in decisions:
            decision.explanation = decision.explanation or "Controlled posture note: assignment recommendation is monitored."
    return schemas.BookingAssignmentReport(
        generated_at=report["generated_at"],
        booking_id=report["booking_id"],
        user_id=report["user_id"],
        current_state=report["current_state"],
        current_assignment_is_current=report["current_assignment_is_current"],
        decisions=decisions,
        control_posture=control_posture,
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
        report = build_booking_assignment_report_from_record(updated_assignment)
    else:
        created_assignment = await create_booking_assignment(
            db,
            booking,
            current_user,
            requested_assignment=requested_assignment,
        )
        report = build_booking_assignment_report_from_record(created_assignment)

    control_posture = _current_control_posture(current_user)
    decisions = [schemas.BookingAssignmentDecision(**decision) for decision in report["decisions"]]
    if control_posture in {"constrained", "observed"}:
        for decision in decisions:
            decision.explanation = decision.explanation or "Controlled posture note: assignment recommendation is monitored."

    return schemas.BookingAssignmentReport(
        generated_at=report["generated_at"],
        booking_id=report["booking_id"],
        user_id=report["user_id"],
        current_state=report["current_state"],
        current_assignment_is_current=report["current_assignment_is_current"],
        decisions=decisions,
        control_posture=control_posture,
    )


@router.get("/{booking_id}/audit", response_model=schemas.BookingAuditSummary)
async def get_booking_audit_summary(
    booking_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    booking_result = await db.execute(
        select(models.Booking).where(
            and_(models.Booking.id == booking_id, models.Booking.user_id == current_user.id)
        )
    )
    booking = booking_result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")

    event_result = await db.execute(
        select(models.BookingEvent)
        .where(models.BookingEvent.booking_id == booking_id)
        .where(models.BookingEvent.user_id == current_user.id)
        .order_by(desc(models.BookingEvent.created_at))
    )
    events = event_result.scalars().all()
    control_posture = _current_control_posture(current_user)
    posture_note = None
    if control_posture in {"constrained", "observed"}:
        posture_note = "Controlled posture note: audit summary reflects monitored access."

    return schemas.BookingAuditSummary(
        booking_id=booking.id,
        user_id=current_user.id,
        total_events=len(events),
        created_events=sum(1 for event in events if event.event_type == "created"),
        status_updates=sum(1 for event in events if event.event_type == "status_updated"),
        latest_event_at=events[0].created_at if events else None,
        latest_event_note=events[0].note if events else None,
        recent_mutation_fields=sorted(
            {
                field
                for event in events[:5]
                for field in (
                    [] if not event.note else [field for field in ("title", "details", "scheduled_date", "service_type", "status") if field in event.note]
                )
            }
        ),
        control_posture=control_posture,
    )


@router.get("/analytics/summary", response_model=schemas.AdminAnalyticsSummary)
async def get_booking_analytics_summary(
    current_user: models.User = Depends(deps.get_current_policy_admin_user),
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

    status_counts = {
        str(getattr(status_value, "value", status_value)): int(count or 0)
        for status_value, count in status_rows.all()
    }
    bookings_by_status = {
        status.value: status_counts.get(status.value, 0)
        for status in models.BookingStatus
    }
    event_counts = [
        schemas.AnalyticsEventCount(event_type=str(event_type), count=int(count or 0))
        for event_type, count in event_rows.all()
    ]
    assignment_events_total = sum(
        count for event_type, count in event_rows.all() if str(event_type).startswith("assignment_")
    )

    return schemas.AdminAnalyticsSummary(
        generated_at=now,
        total_users=int(total_users_result.scalar() or 0),
        total_bookings=int(total_bookings_result.scalar() or 0),
        bookings_by_status=bookings_by_status,
        booking_events_total=int(total_events_result.scalar() or 0),
        assignment_events_total=int(assignment_events_total or 0),
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
    current_user: models.User = Depends(deps.get_current_policy_admin_user),
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

    status_counts = {
        "pending": 0,
        "confirmed": 0,
        "cancelled": 0,
        "completed": 0,
    }
    for event in events:
        status_key = str(getattr(event.to_status, "value", event.to_status or "unknown"))
        status_counts[status_key] = status_counts.get(status_key, 0) + 1

    assignment_events = sum(1 for event in events if event.event_type.startswith("assignment_"))

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
    )


@router.get("/analytics/export", response_model=schemas.BookingExportReport)
async def export_booking_data(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    current_user: models.User = Depends(deps.get_current_policy_admin_user),
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

    return schemas.BookingExportReport(
        generated_at=datetime.now(timezone.utc),
        total_bookings=len(bookings),
        total_events=len(events),
        assignment_events_total=assignment_events_total,
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