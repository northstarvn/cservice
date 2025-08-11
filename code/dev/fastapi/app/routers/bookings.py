from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_, desc, func
from typing import Optional
from app import models, schemas, deps

router = APIRouter()

@router.post("/", response_model=schemas.BookingOut)
async def create_booking(
    booking_in: schemas.BookingCreate,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db)
):
    # Enforce status pending internally (ignore client-sent status to avoid casing issues)
    booking = models.Booking(
        user_id=current_user.id,
        service_type=booking_in.service_type,  # Already normalized by schema
        title=booking_in.title,
        details=booking_in.details,
        scheduled_date=booking_in.scheduled_date,
        status=models.BookingStatus.pending
    )
    db.add(booking)
    await db.commit()
    await db.refresh(booking)
    return booking

@router.get("/", response_model=schemas.PaginatedBookings)
async def get_user_bookings(
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
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
    result = await db.execute(
        select(models.Booking).where(
            and_(models.Booking.id == booking_id, models.Booking.user_id == current_user.id)
        )
    )
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")

    data = booking_update.dict(exclude_unset=True)
    columns = {c.name for c in models.Booking.__table__.columns}

    for field, value in data.items():
        if field in columns and value is not None:
            if hasattr(value, "value"):
                setattr(booking, field, value.value)
            else:
                setattr(booking, field, value)

    await db.commit()
    await db.refresh(booking)
    return booking

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

    await db.delete(booking)
    await db.commit()
    return {"message": "Booking deleted successfully"}