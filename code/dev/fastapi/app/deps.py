from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.db import get_db
from app import models, security
from app.services.policy_scoring import can_access_functionality, upsert_customer_policy_score
import jwt

# Security scheme
security_scheme = HTTPBearer()


def current_control_posture(current_user: models.User) -> str:
    """Resolve a user's control posture, defaulting to 'observed' when unknown."""
    policy_score = getattr(current_user, "policy_score", None)
    return getattr(policy_score, "control_posture", "observed") if policy_score else "observed"

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    db: AsyncSession = Depends(get_db)
) -> models.User:
    """Get current authenticated user from JWT token"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        payload = jwt.decode(
            credentials.credentials,
            security.SECRET_KEY,
            algorithms=[security.ALGORITHM]
        )
    except Exception:
        raise credentials_exception

    username = payload.get("sub")
    if not username:
        raise credentials_exception
    
    # Get user from database
    query = select(models.User).where(models.User.username == username)
    result = await db.execute(query)
    user = result.scalar_one_or_none()
    
    if user is None:
        raise credentials_exception
    return user


async def get_current_admin_user(current_user: models.User = Depends(get_current_user)) -> models.User:
    if not bool(getattr(current_user, "is_admin", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return current_user


async def get_current_policy_or_admin_user(
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> models.User:
    policy_score = await upsert_customer_policy_score(db, current_user)
    if bool(getattr(current_user, "is_admin", False)) or can_access_functionality(policy_score, required_tier="system-premium"):
        return current_user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Policy-controlled admin access required",
    )


async def get_current_customer_policy_score(
    current_user: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> models.CustomerPolicyScore:
    return await upsert_customer_policy_score(db, current_user)