from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os

from jose import jwt
from passlib.context import CryptContext

SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key")  # Use env var in production
ALGORITHM = "HS256"

# Token lifetime configuration (env-tunable).
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "120"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

# Token type claims. Access tokens drive authenticated API calls; refresh
# tokens are longer-lived and intended for silent re-authentication only.
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def _build_token(data: dict, token_type: str, expires_delta: timedelta | None) -> str:
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta is None:
        if token_type == TOKEN_TYPE_REFRESH:
            expires_delta = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        else:
            expires_delta = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": now + expires_delta, "iat": now, "typ": token_type})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(data: dict, expires_delta: timedelta = None):
    """Issue a short-lived access token carrying the ``typ=access`` claim."""
    return _build_token(data, TOKEN_TYPE_ACCESS, expires_delta)


def create_refresh_token(data: dict, expires_delta: timedelta = None):
    """Issue a long-lived refresh token carrying the ``typ=refresh`` claim."""
    return _build_token(data, TOKEN_TYPE_REFRESH, expires_delta)


def decode_access_token(token: str, expected_type: str = TOKEN_TYPE_ACCESS) -> dict:
    """Decode and validate a JWT.

    Raises ``ValueError`` when the token is malformed, expired, missing its
    subject, or carries a token type that does not match ``expected_type``.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception as exc:  # jose raises several subclasses on failure
        raise ValueError("Could not validate credentials") from exc
    token_type = payload.get("typ", TOKEN_TYPE_ACCESS)
    if token_type != expected_type:
        raise ValueError(f"Expected {expected_type} token, got {token_type}")
    if not payload.get("sub"):
        raise ValueError("Token missing subject")
    return payload


# --- Password strength policy -------------------------------------------------
#
# Purely additive guardrail: the backend can assess a proposed password before
# persisting it. Wire enforcement into flows when the product wants it; the
# helpers themselves never mutate state.


@dataclass(frozen=True)
class PasswordPolicy:
    min_length: int = 8
    max_length: int = 128
    require_letter: bool = True
    require_digit: bool = False
    require_special: bool = False


DEFAULT_PASSWORD_POLICY = PasswordPolicy()


def validate_password_strength(
    password: str, policy: PasswordPolicy = DEFAULT_PASSWORD_POLICY
) -> list[str]:
    """Return a list of policy violations (empty means the password is accepted)."""
    if password is None:
        return ["password is required"]
    issues: list[str] = []
    if len(password) < policy.min_length:
        issues.append(f"at least {policy.min_length} characters")
    if len(password) > policy.max_length:
        issues.append(f"no more than {policy.max_length} characters")
    if policy.require_letter and not any(ch.isalpha() for ch in password):
        issues.append("at least one letter")
    if policy.require_digit and not any(ch.isdigit() for ch in password):
        issues.append("at least one digit")
    if policy.require_special and not any(not ch.isalnum() for ch in password):
        issues.append("at least one special character")
    return issues


def password_policy_payload(policy: PasswordPolicy = DEFAULT_PASSWORD_POLICY) -> dict:
    """Introspectable policy description for metadata/catalog endpoints."""
    return {
        "min_length": policy.min_length,
        "max_length": policy.max_length,
        "require_letter": policy.require_letter,
        "require_digit": policy.require_digit,
        "require_special": policy.require_special,
        "hash_scheme": "bcrypt",
        "token_algorithm": ALGORITHM,
        "access_token_expire_minutes": ACCESS_TOKEN_EXPIRE_MINUTES,
        "refresh_token_expire_days": REFRESH_TOKEN_EXPIRE_DAYS,
    }