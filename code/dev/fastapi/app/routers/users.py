from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError
from app import models, security, deps
from app.i18n import locale_payload
from app.schemas import schemas
from app.services.policy_scoring import can_access_functionality

router = APIRouter()


def _current_control_posture(current_user: models.User) -> str:
    policy_score = getattr(current_user, "policy_score", None)
    return getattr(policy_score, "control_posture", "observed") if policy_score else "observed"

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
        "locale": locale_payload(getattr(user_credentials, "locale", None)),
    }

@router.get("/me", response_model=schemas.UserOut)
async def read_users_me(current_user: models.User = Depends(deps.get_current_user)):
    control_posture = _current_control_posture(current_user)
    if control_posture in {"high_trust", "customer_trusted"}:
        current_user.full_name = current_user.full_name or current_user.username
    return current_user


@router.get("/me/policy-score", response_model=schemas.CustomerPolicyScoreOut)
async def read_users_policy_score(
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
):
    return policy_score


@router.get("/me/can-access", response_model=schemas.CustomerPolicyAccessOut)
async def read_users_access_decision(
    functionality: str,
    required_tier: str = "standard",
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
):
    control_posture = getattr(policy_score, "control_posture", "observed")
    effective_required_tier = required_tier
    if control_posture in {"constrained", "observed"} and required_tier == "standard":
        effective_required_tier = "customer-premium"

    allowed = can_access_functionality(policy_score, required_tier=effective_required_tier)
    return {
        "user_id": current_user.id,
        "functionality": functionality,
        "required_tier": required_tier,
        "effective_required_tier": effective_required_tier,
        "allowed": allowed,
        "policy_tier": policy_score.policy_tier,
        "control_posture": control_posture,
        "access_score": policy_score.access_score,
        "customer_score": policy_score.customer_score,
        "system_score": policy_score.system_score,
    }


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

    control_posture = _current_control_posture(current_user)
    if control_posture in {"high_trust", "customer_trusted"}:
        message = "Password updated successfully."
    elif control_posture in {"constrained", "observed"}:
        message = "Password updated successfully under controlled access posture."
    else:
        message = "Password updated successfully."

    return {"message": message}