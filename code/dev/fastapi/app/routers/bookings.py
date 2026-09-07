from fastapi import APIRouter, Depends, HTTPException, status, Query
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
    create_booking_event,
    ensure_booking_transition_allowed,
    get_owned_booking,
    touch_booking,
    transition_booking,
)

router = APIRouter()

@router.post("/", response_model=schemas.BookingOut)
async def create_booking(
    booking: schemas.BookingCreate,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db)
):
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
    status: Optional[schemas.BookingStatus] = None,
    service_type: Optional[schemas.ServiceType] = None,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db)
):
    query = select(models.Booking).where(models.Booking.user_id == current_user.id)
    if status:
        query = query.where(models.Booking.status == status.value)
    if service_type:
        query = query.where(models.Booking.service_type == service_type.value)

    count_query = select(func.count(models.Booking.id)).where(models.Booking.user_id == current_user.id)
    if status:
        count_query = count_query.where(models.Booking.status == status.value)
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
    db: AsyncSession = Depends(deps.get_db)
):
    result = await db.execute(
        select(models.Booking).where(
            and_(models.Booking.id == booking_id, models.Booking.user_id == current_user.id)
        )
    )
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    return booking

@router.put("/{booking_id}", response_model=schemas.BookingOut)
async def update_booking(
    booking_id: int,
    booking_update: schemas.BookingUpdate,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db)
):
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
        old_status = booking.status
        history_fields = {"title", "details", "scheduled_date", "service_type"}
        history_changed = any(field in update_data and getattr(booking, field) != update_data[field] for field in history_fields)
        
        apply_booking_updates(booking, update_data)
        touch_booking(booking)

        if history_changed:
            await create_booking_event(
                db,
                booking,
                current_user,
                event_type="updated",
                note="Booking details updated",
                from_status=old_status,
                to_status=booking.status,
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
    db: AsyncSession = Depends(deps.get_db)
):
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
        note="Booking deleted",
        from_status=booking.status,
        to_status=booking.status,
    )
    await db.delete(booking)
    await db.commit()
    return {"message": "Booking deleted successfully"}


@router.get("/{booking_id}/history", response_model=schemas.BookingHistoryReport)
async def get_booking_history(
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

    return schemas.BookingHistoryReport(
        booking_id=booking.id,
        user_id=current_user.id,
        current_status=booking.status,
        event_count=len(events),
        items=events,
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

    return schemas.BookingAuditSummary(
        booking_id=booking.id,
        user_id=current_user.id,
        total_events=len(events),
        created_events=sum(1 for event in events if event.event_type == "created"),
        status_updates=sum(1 for event in events if event.event_type == "status_updated"),
        latest_event_at=events[0].created_at if events else None,
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

    return schemas.AdminAnalyticsSummary(
        generated_at=now,
        total_users=int(total_users_result.scalar() or 0),
        total_bookings=int(total_bookings_result.scalar() or 0),
        bookings_by_status=bookings_by_status,
        booking_events_total=int(total_events_result.scalar() or 0),
        booking_events_by_type=event_counts,
        recent_bookings=int(recent_bookings_result.scalar() or 0),
        recent_events=int(recent_events_result.scalar() or 0),
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

    status_counts = {
        "pending": 0,
        "confirmed": 0,
        "cancelled": 0,
        "completed": 0,
    }
    for event in events:
        status_key = str(getattr(event.to_status, "value", event.to_status or "unknown"))
        status_counts[status_key] = status_counts.get(status_key, 0) + 1

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

    return schemas.BookingExportReport(
        generated_at=datetime.now(timezone.utc),
        total_bookings=len(bookings),
        total_events=len(events),
        bookings=bookings,
        events=events,
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