from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app import models, schemas, security, deps

router = APIRouter()

@router.post("/register", response_model=schemas.UserOut)
async def register(user_in: schemas.UserCreate, db: AsyncSession = Depends(deps.get_db)):
    q = select(models.User).where(models.User.username == user_in.username)
    res = await db.execute(q)
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already registered")

    user = models.User(
        username=user_in.username,
        hashed_password=security.get_password_hash(user_in.password),
        full_name=user_in.full_name,
        email=user_in.email,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user

@router.post("/login", response_model=schemas.Token)
async def login(form_data: schemas.UserCreate, db: AsyncSession = Depends(deps.get_db)):
    q = select(models.User).where(models.User.username == form_data.username)
    res = await db.execute(q)
    user = res.scalar_one_or_none()
    if not user or not security.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    access_token = security.create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}

@router.get("/me", response_model=schemas.UserOut)
async def read_users_me(current_user: models.User = Depends(deps.get_current_user)):
    return current_user