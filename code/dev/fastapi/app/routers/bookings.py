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
    """Create a new booking for the current user."""
    booking = models.Booking(
        user_id=current_user.id,
        service_type=booking_in.service_type,
        title=booking_in.title,                    # ✅ Correct field
        description=booking_in.description,        # ✅ Correct field  
        scheduled_date=booking_in.scheduled_date   # ✅ Correct field
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
    service_type: Optional[str] = None,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db)
):
    """Get paginated bookings for the current user."""
    query = select(models.Booking).where(models.Booking.user_id == current_user.id)
    
    if status:
        query = query.where(models.Booking.status == status)
    if service_type:
        query = query.where(models.Booking.service_type == service_type)
    
    # Count total
    count_query = select(func.count(models.Booking.id)).where(models.Booking.user_id == current_user.id)
    if status:
        count_query = count_query.where(models.Booking.status == status)
    if service_type:
        count_query = count_query.where(models.Booking.service_type == service_type)
    
    total_result = await db.execute(count_query)
    total = total_result.scalar()
    
    # Calculate offset
    offset = (page - 1) * per_page
    
    # Get paginated results
    query = query.order_by(desc(models.Booking.created_at)).offset(offset).limit(per_page)
    result = await db.execute(query)
    bookings = result.scalars().all()
    
    pages = (total + per_page - 1) // per_page
    
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
    """Get a specific booking by ID."""
    query = select(models.Booking).where(
        and_(
            models.Booking.id == booking_id,
            models.Booking.user_id == current_user.id
        )
    )
    result = await db.execute(query)
    booking = result.scalar_one_or_none()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found"
        )
    
    return booking

@router.put("/{booking_id}", response_model=schemas.BookingOut)
async def update_booking(
    booking_id: int,
    booking_update: schemas.BookingUpdate,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db)
):
    """Update a specific booking."""
    query = select(models.Booking).where(
        and_(
            models.Booking.id == booking_id,
            models.Booking.user_id == current_user.id
        )
    )
    result = await db.execute(query)
    booking = result.scalar_one_or_none()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found"
        )
    
    update_data = booking_update.dict(exclude_unset=True)
    for field, value in update_data.items():
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
    """Delete a specific booking."""
    query = select(models.Booking).where(
        and_(
            models.Booking.id == booking_id,
            models.Booking.user_id == current_user.id
        )
    )
    result = await db.execute(query)
    booking = result.scalar_one_or_none()
    
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking not found"
        )
    
    await db.delete(booking)
    await db.commit()
    return {"message": "Booking deleted successfully"}