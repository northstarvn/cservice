from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_, desc, func
from sqlalchemy.exc import IntegrityError
from typing import Optional
from datetime import datetime
from app import models, schemas, deps

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

        # Get update data using model_dump instead of deprecated dict()
        update_data = booking_update.model_dump(exclude_unset=True, exclude_none=True)
        
        # Update fields with proper enum handling
        for field, value in update_data.items():
            if hasattr(booking, field):
                if field in ['service_type', 'status']:
                    # Handle enum values properly
                    if hasattr(value, 'value'):
                        setattr(booking, field, value.value)
                    elif isinstance(value, str):
                        # Convert string to enum if needed
                        setattr(booking, field, value)
                    else:
                        setattr(booking, field, value)
                else:
                    setattr(booking, field, value)
        
        # Ensure updated_at is set
        booking.updated_at = datetime.utcnow()
        
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

    await db.delete(booking)
    await db.commit()
    return {"message": "Booking deleted successfully"}