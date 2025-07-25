from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app import models, schemas, deps
from typing import List, Optional
from datetime import datetime

router = APIRouter()

@router.post("/", response_model=schemas.BookingOut)
async def create_booking(
    booking_in: schemas.BookingCreate,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    booking = models.Booking(
        user_id=current_user.id,
        **booking_in.dict(),
        status=models.BookingStatus.pending
    )
    db.add(booking)
    await db.commit()
    await db.refresh(booking)
    return booking

@router.get("/", response_model=List[schemas.BookingOut])
async def list_bookings(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    skip: int = 0,
    limit: int = 20,
    status: Optional[schemas.BookingStatus] = Query(None)
):
    q = select(models.Booking).where(models.Booking.user_id == current_user.id)
    if status:
        q = q.where(models.Booking.status == status)
    q = q.offset(skip).limit(limit).order_by(models.Booking.scheduled_for.desc())
    res = await db.execute(q)
    return res.scalars().all()

@router.get("/{booking_id}", response_model=schemas.BookingOut)
async def get_booking(
    booking_id: int,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    booking = await db.get(models.Booking, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking

@router.patch("/{booking_id}", response_model=schemas.BookingOut)
async def update_booking(
    booking_id: int,
    booking_in: schemas.BookingUpdate,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    booking = await db.get(models.Booking, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")
    for field, value in booking_in.dict(exclude_unset=True).items():
        setattr(booking, field, value)
    await db.commit()
    await db.refresh(booking)
    return booking

@router.delete("/{booking_id}", status_code=204)
async def delete_booking(
    booking_id: int,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    booking = await db.get(models.Booking, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")
    await db.delete(booking)
    await db.commit()
    return