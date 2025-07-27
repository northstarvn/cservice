from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_, or_, func
from app import models, schemas, deps
from typing import List, Optional
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/", response_model=schemas.BookingOut, status_code=status.HTTP_201_CREATED)
async def create_booking(
    booking_in: schemas.BookingCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    """Create a new booking with validation and notifications"""
    try:
        # Check if the scheduled time is in the future
        if booking_in.scheduled_for <= datetime.utcnow():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Booking must be scheduled for a future date and time"
            )
        
        # Check for conflicts (optional - if you want to prevent double booking)
        conflict_query = select(models.Booking).where(
            and_(
                models.Booking.scheduled_for == booking_in.scheduled_for,
                models.Booking.status.in_([models.BookingStatus.pending, models.BookingStatus.confirmed])
            )
        )
        conflict_result = await db.execute(conflict_query)
        if conflict_result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This time slot is already booked"
            )
        
        booking = models.Booking(
            user_id=current_user.id,
            **booking_in.dict(),
            status=models.BookingStatus.pending,
            created_at=datetime.utcnow()
        )
        db.add(booking)
        await db.commit()
        await db.refresh(booking)
        
        # Add background task for email notification
        background_tasks.add_task(
            send_booking_confirmation_email,
            current_user.email,
            booking
        )
        
        logger.info(f"Booking created: {booking.id} for user {current_user.id}")
        return booking
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating booking: {str(e)}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create booking"
        )

@router.get("/", response_model=schemas.PaginatedBookings)
async def list_bookings(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(20, ge=1, le=100, description="Number of records to return"),
    status: Optional[schemas.BookingStatus] = Query(None, description="Filter by booking status"),
    service_type: Optional[str] = Query(None, description="Filter by service type"),
    date_from: Optional[datetime] = Query(None, description="Filter bookings from this date"),
    date_to: Optional[datetime] = Query(None, description="Filter bookings until this date")
):
    """Get paginated list of user's bookings with filters"""
    try:
        # Base query
        base_query = select(models.Booking).where(models.Booking.user_id == current_user.id)
        
        # Apply filters
        if status:
            base_query = base_query.where(models.Booking.status == status)
        if service_type:
            base_query = base_query.where(models.Booking.service_type.ilike(f"%{service_type}%"))
        if date_from:
            base_query = base_query.where(models.Booking.scheduled_for >= date_from)
        if date_to:
            base_query = base_query.where(models.Booking.scheduled_for <= date_to)
        
        # Get total count
        count_query = select(func.count()).select_from(base_query.subquery())
        count_result = await db.execute(count_query)
        total = count_result.scalar()
        
        # Apply pagination and ordering
        query = base_query.offset(skip).limit(limit).order_by(models.Booking.scheduled_for.desc())
        result = await db.execute(query)
        bookings = result.scalars().all()
        
        return schemas.PaginatedBookings(
            items=bookings,
            total=total,
            page=(skip // limit) + 1,
            per_page=limit,
            pages=(total + limit - 1) // limit
        )
        
    except Exception as e:
        logger.error(f"Error listing bookings: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve bookings"
        )

@router.get("/{booking_id}", response_model=schemas.BookingOut)
async def get_booking(
    booking_id: int,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    """Get a specific booking by ID"""
    try:
        booking = await db.get(models.Booking, booking_id)
        if not booking or booking.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, 
                detail="Booking not found"
            )
        return booking
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting booking {booking_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve booking"
        )

@router.patch("/{booking_id}", response_model=schemas.BookingOut)
async def update_booking(
    booking_id: int,
    booking_in: schemas.BookingUpdate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    """Update a booking with validation"""
    try:
        booking = await db.get(models.Booking, booking_id)
        if not booking or booking.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Booking not found"
            )
        
        # Prevent updates to cancelled or completed bookings
        if booking.status in [models.BookingStatus.cancelled, models.BookingStatus.completed]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot update {booking.status} booking"
            )
        
        # Validate future date if scheduled_for is being updated
        update_data = booking_in.dict(exclude_unset=True)
        if 'scheduled_for' in update_data:
            if update_data['scheduled_for'] <= datetime.utcnow():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Booking must be scheduled for a future date and time"
                )
        
        # Apply updates
        for field, value in update_data.items():
            setattr(booking, field, value)
        
        booking.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(booking)
        
        # Send update notification
        background_tasks.add_task(
            send_booking_update_email,
            current_user.email,
            booking
        )
        
        logger.info(f"Booking updated: {booking.id}")
        return booking
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating booking {booking_id}: {str(e)}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update booking"
        )

@router.delete("/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_booking(
    booking_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    """Cancel/delete a booking with business rules"""
    try:
        booking = await db.get(models.Booking, booking_id)
        if not booking or booking.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Booking not found"
            )
        
        # Check if booking can be cancelled (e.g., not within 2 hours of appointment)
        time_until_booking = booking.scheduled_for - datetime.utcnow()
        if time_until_booking < timedelta(hours=2):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot cancel booking less than 2 hours before the scheduled time"
            )
        
        # Instead of deleting, mark as cancelled for audit trail
        booking.status = models.BookingStatus.cancelled
        booking.updated_at = datetime.utcnow()
        await db.commit()
        
        # Send cancellation notification
        background_tasks.add_task(
            send_cancellation_email,
            current_user.email,
            booking
        )
        
        logger.info(f"Booking cancelled: {booking.id}")
        return
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling booking {booking_id}: {str(e)}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel booking"
        )

@router.get("/available-slots/{date}")
async def get_available_slots(
    date: str,  # Format: YYYY-MM-DD
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    """Get available time slots for a specific date"""
    try:
        # Parse the date
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        
        # Business hours: 9 AM to 5 PM, 1-hour slots
        business_hours = [f"{hour:02d}:00" for hour in range(9, 17)]
        
        # Get booked slots for the date
        start_of_day = datetime.combine(target_date, datetime.min.time())
        end_of_day = datetime.combine(target_date, datetime.max.time())
        
        booked_query = select(models.Booking.scheduled_for).where(
            and_(
                models.Booking.scheduled_for >= start_of_day,
                models.Booking.scheduled_for <= end_of_day,
                models.Booking.status.in_([models.BookingStatus.pending, models.BookingStatus.confirmed])
            )
        )
        booked_result = await db.execute(booked_query)
        booked_times = {booking.scheduled_for.strftime("%H:%M") for booking in booked_result.scalars()}
        
        # Return available slots
        available_slots = [slot for slot in business_hours if slot not in booked_times]
        
        return {
            "date": date,
            "available_slots": available_slots,
            "booked_slots": list(booked_times)
        }
        
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid date format. Use YYYY-MM-DD"
        )
    except Exception as e:
        logger.error(f"Error getting available slots for {date}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve available slots"
        )

# Background task functions (add these to a separate tasks module)
async def send_booking_confirmation_email(email: str, booking: models.Booking):
    """Send booking confirmation email"""
    logger.info(f"Sending confirmation email for booking {booking.id} to {email}")
    # Implement actual email sending logic here

async def send_booking_update_email(email: str, booking: models.Booking):
    """Send booking update email"""
    logger.info(f"Sending update email for booking {booking.id} to {email}")
    # Implement actual email sending logic here

async def send_cancellation_email(email: str, booking: models.Booking):
    """Send booking cancellation email"""
    logger.info(f"Sending cancellation email for booking {booking.id} to {email}")
    # Implement actual email sending logic here