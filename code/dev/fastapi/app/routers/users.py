from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError
from app import models, security, deps
from app.schemas import schemas

router = APIRouter()

@router.post("/register", response_model=schemas.UserOut)
async def register(user_in: schemas.UserCreate, db: AsyncSession = Depends(deps.get_db)):
    # Check if username already exists
    q = select(models.User).where(models.User.username == user_in.username)
    res = await db.execute(q)
    if res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Username already registered"
        )
    
    # Check if email already exists
    q_email = select(models.User).where(models.User.email == user_in.email)
    res_email = await db.execute(q_email)
    if res_email.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Email already registered"
        )

    try:
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
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed. Please try again."
        )

@router.post("/login", response_model=schemas.Token)
async def login(user_credentials: schemas.UserLogin, db: AsyncSession = Depends(deps.get_db)):
    q = select(models.User).where(models.User.username == user_credentials.username)
    res = await db.execute(q)
    user = res.scalar_one_or_none()
    
    if not user or not security.verify_password(user_credentials.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token = security.create_access_token(data={"sub": user.username})
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": security.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }

@router.get("/me", response_model=schemas.UserOut)
async def read_users_me(current_user: models.User = Depends(deps.get_current_user)):
    return current_user


@router.post("/me/password", response_model=schemas.PasswordChangeResult)
async def change_password(
    payload: schemas.PasswordChange,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    if not security.verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    if payload.current_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the current password",
        )

    current_user.hashed_password = security.get_password_hash(payload.new_password)
    await db.commit()

    return {"message": "Password updated successfully"}