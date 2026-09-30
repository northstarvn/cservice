from datetime import datetime, timezone
from typing import Any, Iterable

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError
from app import models, security, deps
from app.i18n import locale_payload
from app.schemas import schemas
from app.services.policy_scoring import build_policy_access_decision, build_policy_decision_report, build_customer_policy_snapshot, build_policy_score_out, can_access_functionality

router = APIRouter()


_current_control_posture = deps.current_control_posture

# --- Additive session / credential policy tables -----------------------------
#
# Everything below the historical register/login/me surface is config driven so
# deployments can tighten or relax it without a code change.

# What a step-up token is for, keyed by the level it grants.
STEP_UP_PURPOSE: dict[str, str] = {
    "none": "baseline session",
    "loa1": "single-factor confirmation",
    "loa2": "elevated write operations",
    "loa3": "privileged and irreversible operations",
}

# Evidence methods accepted at each level. A level is satisfied when the
# presented token carries a method in its list, or one from a lower level.
STEP_UP_METHODS: dict[str, tuple[str, ...]] = {
    "loa1": ("pwd",),
    "loa2": ("pwd", "mfa", "otp", "hwk"),
    "loa3": ("mfa", "hwk", "biometric", "cosign"),
}

# Scopes an operator may self-assign to a machine credential. ``*`` is never
# self-assignable — it is for bootstrap keys injected through the environment.
API_KEY_SELF_SCOPES: frozenset[str] = frozenset(
    {"read", "write", "chat", "bookings", "audit", "read:chat", "read:audit"}
)
API_KEY_MAX_TTL_DAYS = 365
API_KEY_MISSING_SCOPES: tuple[str, ...] = ("read",)

# Ordered advice shown by ``GET /users/me/password-policy``. Every entry is
# keyed by the policy field that turns it on, so a tightened policy produces
# coaching and a loose one does not.
PASSWORD_ADVICE: tuple[tuple[str, str], ...] = (
    ("require_digit", "Mix in digits to widen the search space."),
    ("require_special", "Add a symbol to widen the search space further."),
    ("require_upper", "Mix upper and lower case rather than all lower."),
    ("forbid_common", "Avoid dictionary and brand passwords."),
    ("forbid_sequences", "Avoid runs like ``abcd`` or ``1234``."),
    ("forbid_repeated_chars", "Avoid repeating one character three or more times."),
    ("min_strength", "Aim for a strength score near the top of the 0-4 scale."),
    ("history_depth", "Pick something you have not used on this account before."),
)

# Recommendation templates used by the security-posture read model.
POSTURE_RECOMMENDATIONS: tuple[tuple[str, str, str], ...] = (
    # (condition key, recommendation, severity)
    ("no_active_sessions", "This subject has no live session tracked right now.", "info"),
    ("no_api_keys", "No machine credentials are registered for this subject.", "info"),
    ("no_step_up", "The presented token carries no step-up assurance.", "warning"),
    ("has_expired_api_keys", "One or more machine credentials have expired and should be revoked.", "warning"),
    ("low_trust", "The control posture is constrained; keep write operations step-up gated.", "info"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: Any) -> datetime | None:
    """Coerce a claim/datetime/string into an aware UTC ``datetime``."""
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    if isinstance(moment, (int, float)):
        return datetime.fromtimestamp(moment, tz=timezone.utc)
    if isinstance(moment, str):
        text = moment.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def build_session_record(
    claims: dict,
    *,
    revoked: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize token claims into one session row for the inventory read model.

    Pure: it never touches the registry, so callers can build rows for tokens
    that are *not* in the deny-list (e.g. the token that authenticated the
    call) and still get a comparable shape.
    """
    moment = now or _now()
    issued = _as_utc(claims.get("iat"))
    expires = _as_utc(claims.get("exp"))
    return {
        "jti": claims.get("jti") or "",
        "token_type": claims.get("typ", security.TOKEN_TYPE_ACCESS),
        "subject": claims.get("sub") or "",
        "issued_at": issued,
        "expires_at": expires,
        "revoked": bool(revoked),
        "active": not revoked and (expires is None or expires > moment),
    }


def build_session_inventory(
    subject: str,
    rows: Iterable[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fold session records into the inventory payload, newest first.

    Rows whose issue time is unknown sort last: an unverifiable age is not
    evidence of recency, and burying it keeps the live head of the list honest.
    """
    # A sort sentinel just before the epoch, so a row with no `issued_at` lands
    # last under `reverse=True` without needing a real date. Ruff's F841 does not
    # count a reference from inside the lambda below as a use, so it reports this
    # as dead code; it is not, and removing it raises NameError at runtime.
    unknown = datetime.min.replace(tzinfo=timezone.utc)
    sessions = sorted(
        (dict(row) for row in rows if row.get("subject") == subject or not row.get("subject")),
        key=lambda row: (row.get("issued_at") or unknown, row.get("jti") or ""),
        reverse=True,
    )
    return {
        "subject": subject,
        "sessions": sessions,
        "active_sessions": sum(1 for row in sessions if row.get("active")),
        "revoked_sessions": sum(1 for row in sessions if row.get("revoked")),
        "mechanism": "jti deny-list, TTL-bounded",
    }


def build_refresh_plan(
    validation: security.TokenValidation,
    *,
    rotate: bool = True,
    locale: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure description of what a refresh should mint, before anything is minted.

    ``rotated`` says whether the presented refresh token gets burned; the
    endpoint only calls ``REVOCATIONS.revoke`` when that is true, so a client
    that opts out of rotation keeps a reusable refresh token.
    """
    claims = dict(validation.claims or {})
    # Carry the subject and any tenant/delegation context forward verbatim so
    # a refreshed token is not less capable than the one it replaces.
    data: dict[str, Any] = {"sub": validation.subject}
    for carried in ("tenant_id", "act", "amr", "acr", "scopes", "roles"):
        if claims.get(carried) is not None:
            data[carried] = claims[carried]
    return {
        "data": data,
        "rotate": bool(rotate),
        "access_expires_in": security.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "refresh_expires_in": security.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        "previous_jti": validation.jti,
        "previous_token_type": validation.token_type or security.TOKEN_TYPE_REFRESH,
        "expires_at": _as_utc(claims.get("exp")) or (now or _now()),
        "locale": locale_payload(locale),
    }


def build_step_up_plan(
    level: str,
    *,
    method: str = "mfa",
    current_level: str = "none",
) -> dict[str, Any]:
    """Describe a step-up request: what was asked for and what it unlocks.

    Unknown levels are reported as unsatisfied rather than raising, so a client
    probing ahead of the server's vocabulary gets a usable answer.
    """
    requested = (level or "none").strip().lower()
    known = requested in STEP_UP_PURPOSE
    methods = STEP_UP_METHODS.get(requested, ())
    already = security.step_up_level_of({"acr": current_level}) if current_level else "none"
    return {
        "requested_level": requested,
        "known_level": known,
        "purpose": STEP_UP_PURPOSE.get(requested, "unrecognised assurance level"),
        "accepted_methods": list(methods),
        "method_accepted": method in methods,
        "already_satisfied": known and security.step_up_satisfied({"acr": already}, requested),
        "current_level": already,
    }


def policy_requirements(policy: security.PasswordPolicy = security.DEFAULT_PASSWORD_POLICY) -> list[str]:
    """Human-readable requirements for a password policy, in declaration order."""
    labels = {
        "min_length": f"at least {policy.min_length} characters",
        "max_length": f"at most {policy.max_length} characters",
        "require_letter": "at least one letter",
        "require_digit": "at least one digit",
        "require_special": "at least one special character",
        "require_upper": "at least one uppercase letter",
        "forbid_common": "not a commonly used password",
        "forbid_repeated_chars": "no more than two identical characters in a row",
        "forbid_sequences": "no keyboard or alphabet sequences",
        "min_strength": f"a strength score of at least {policy.min_strength} (0-4)",
    }
    return [label for field, label in labels.items() if getattr(policy, field)]


def policy_advice(policy: security.PasswordPolicy = security.DEFAULT_PASSWORD_POLICY) -> list[str]:
    """Ordered coaching hints derived from the *active* policy only.

    The length hint is rendered from the live minimum so the advice cannot
    drift away from the rule it is explaining.
    """
    advice: list[str] = []
    if policy.min_length:
        advice.append(
            f"Use at least {policy.min_length} characters so length, not symbols, "
            "carries the strength."
        )
    advice.extend(text for field, text in PASSWORD_ADVICE if getattr(policy, field))
    return advice


def filter_self_scopes(requested: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split requested scopes into ``(granted, rejected)`` for self-service.

    Self-issued credentials never receive ``*``: a user minting a key must not
    be able to mint a key that outranks their own session.
    """
    granted: list[str] = []
    rejected: list[str] = []
    for raw in requested or ():
        scope = str(raw).strip()
        if not scope:
            continue
        if scope == "*" or scope not in API_KEY_SELF_SCOPES:
            rejected.append(scope)
        elif scope not in granted:
            granted.append(scope)
    return sorted(granted), sorted(set(rejected))


def build_api_key_payload(record: security.ApiKeyRecord, plaintext: str | None = None) -> dict[str, Any]:
    """Public view of a machine credential. The secret only ever rides along once."""
    return {
        "key_id": record.key_id,
        "name": record.name,
        "subject": record.subject,
        "scopes": list(record.scopes),
        "hash_prefix": record.hash_prefix,
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "revoked": record.revoked,
        "plaintext": plaintext,
    }


def build_password_feedback(
    candidate: str,
    *,
    username: str | None = None,
    policy: security.PasswordPolicy = security.DEFAULT_PASSWORD_POLICY,
    history: security.PasswordHistory | None = None,
) -> dict[str, Any]:
    """Non-mutating assessment of a proposed password.

    Never raises: a weak candidate is data, not an error. Reuse detection is
    delegated to the configured history ring, which is a no-op at depth 0.
    """
    text = candidate or ""
    score = security.score_password_strength(text)
    issues = security.validate_password_strength(text, policy)
    lowered = text.lower()
    contains_username = bool(username) and username.lower() in lowered and len(username) > 2
    if contains_username:
        issues = [*issues, "must not contain your username"]
    reused = bool(history and history.is_reused(text))
    if reused:
        issues = [*issues, "has been used on this account before"]
    return {
        "accepted": not issues,
        "score": score["score"],
        "entropy_bits": score["entropy_bits"],
        "adjusted_bits": score["adjusted_bits"],
        "factors": list(score["factors"]),
        "penalties": list(score["penalties"]),
        "issues": issues,
        "advice": policy_advice(policy),
        "reused": reused,
        "contains_username": contains_username,
        "policy": security.password_policy_payload(policy),
    }


def build_api_key_inventory(subject: str, records: Iterable[security.ApiKeyRecord], now: datetime | None = None) -> dict[str, Any]:
    """Split a subject's credentials into active/expired/revoked counts."""
    moment = (now or _now()).isoformat()
    rows = [record for record in records if record.subject == subject]
    keys = [build_api_key_payload(record) for record in sorted(rows, key=lambda r: (r.created_at, r.name))]
    expired = sum(1 for r in rows if r.expires_at and not r.revoked and r.expires_at <= moment)
    return {
        "subject": subject,
        "keys": keys,
        "total": len(keys),
        "active": sum(1 for r in rows if not r.revoked) - expired,
        "revoked": sum(1 for r in rows if r.revoked),
        "expired": expired,
    }


def build_security_posture(
    subject: str,
    *,
    control_posture: str = "observed",
    step_up_level: str = "none",
    auth_method: str = deps.AUTH_METHOD_USER,
    sessions: Iterable[dict[str, Any]] = (),
    keys: Iterable[security.ApiKeyRecord] = (),
    is_admin: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read model joining session, credential and assurance state for one subject.

    Recommendations are emitted from a fixed template table so the wording
    cannot drift with the data.
    """
    moment = now or _now()
    session_rows: list[dict[str, Any]] = []
    highest = "none"
    for entry in sessions:
        claims = entry.get("claims") or {}
        revoked = bool(entry.get("revoked"))
        row = build_session_record(claims, revoked=revoked, now=moment)
        session_rows.append(row)
        if not row["active"]:
            continue
        # Assurance is a property of the token, so it is read off the claims
        # rather than assumed from the row shape.
        level = security.step_up_level_of(claims)
        if security.STEP_UP_RANK.get(level, 0) > security.STEP_UP_RANK.get(highest, 0):
            highest = level
    inventory = build_api_key_inventory(subject, keys, now=moment)
    active_sessions = sum(1 for row in session_rows if row["active"])
    flags = {
        "no_active_sessions": active_sessions == 0,
        "no_api_keys": inventory["total"] == 0,
        "no_step_up": security.STEP_UP_RANK.get(step_up_level, 0) == 0,
        "has_expired_api_keys": inventory["expired"] > 0,
        "low_trust": control_posture in {"constrained", "observed"},
    }
    return {
        "subject": subject,
        "control_posture": control_posture,
        "step_up_level": step_up_level or "none",
        "auth_method": auth_method,
        "active_sessions": active_sessions,
        "active_api_keys": inventory["active"],
        "expired_api_keys": inventory["expired"],
        "highest_known_step_up": highest,
        "signed_in_as_admin": bool(is_admin),
        "recommendations": [
            {"condition": key, "severity": severity, "text": text}
            for key, text, severity in POSTURE_RECOMMENDATIONS
            if flags.get(key)
        ],
    }


def build_users_catalog() -> dict[str, Any]:
    """Introspectable catalog of the identity surface (meta tooling)."""
    return {
        "lifecycle": {
            "steps": ["register", "login", "refresh", "logout"],
            "rotation_default": True,
            "refresh_token_expire_days": security.REFRESH_TOKEN_EXPIRE_DAYS,
            "access_token_expire_minutes": security.ACCESS_TOKEN_EXPIRE_MINUTES,
        },
        "sessions": {
            "inventory_source": "presented claims + revocation deny-list",
            "ordering": "issued_at desc, jti desc",
            "mechanism": "jti deny-list, TTL-bounded",
        },
        "step_up": {
            "levels": list(security.STEP_UP_LEVELS),
            "purpose": dict(STEP_UP_PURPOSE),
            "methods": {level: list(methods) for level, methods in STEP_UP_METHODS.items()},
            "claims": ["acr", "amr"],
        },
        "api_keys": {
            "prefix": security.API_KEY_PREFIX,
            "self_service_scopes": sorted(API_KEY_SELF_SCOPES),
            "max_ttl_days": API_KEY_MAX_TTL_DAYS,
            "wildcard_self_assignable": False,
            "storage": "sha256 digest only",
        },
        "password_policy": security.password_policy_payload(),
        "password_requirements": policy_requirements(),
        "password_advice": policy_advice(),
    }


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


@router.get("/me/can-access")
async def read_users_access_decision(
    functionality: str,
    required_tier: str = "standard",
    current_user: models.User = Depends(deps.get_current_user),
    policy_score: models.CustomerPolicyScore = Depends(deps.get_current_customer_policy_score),
    db: AsyncSession = Depends(deps.get_db),
):
    access_decision = build_policy_access_decision(current_user, policy_score, functionality, required_tier)
    control_posture = getattr(policy_score, "control_posture", "observed")
    effective_required_tier = required_tier
    if control_posture in {"constrained", "observed"} and required_tier == "standard":
        effective_required_tier = "customer-premium"
    allowed = can_access_functionality(policy_score, required_tier=effective_required_tier)
    if hasattr(db, "execute"):
        snapshot = await build_customer_policy_snapshot(db, current_user)
        decision_report = build_policy_decision_report(snapshot, current_user, functionality, required_tier)
        return {
            **access_decision.model_dump(),
            "control_posture": control_posture,
            "effective_required_tier": effective_required_tier,
            "allowed": allowed,
            "policy_decision": decision_report,
        }

    return {
        **access_decision.model_dump(),
        "control_posture": control_posture,
        "effective_required_tier": effective_required_tier,
        "allowed": allowed,
    }


@router.get("/me/policy-decision", response_model=schemas.CustomerPolicyDecisionSummaryOut)
async def read_users_policy_decision(
    functionality: str,
    required_tier: str = "standard",
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    snapshot = await build_customer_policy_snapshot(db, current_user)
    decision = build_policy_decision_report(snapshot, current_user, functionality, required_tier)
    return {
        **decision.model_dump(),
        "topic_context": getattr(snapshot, "topic_context", ""),
    }


@router.get("/me/policy-decision/report", response_model=schemas.CustomerPolicyDecisionReportOut)
async def read_users_policy_decision_report(
    functionality: str,
    required_tier: str = "standard",
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    snapshot = await build_customer_policy_snapshot(db, current_user)
    policy_score = build_policy_score_out(snapshot, current_user)
    decision_report = build_policy_decision_report(snapshot, current_user, functionality, required_tier)
    return {
        "policy_decision": decision_report,
        "policy_score": policy_score,
        "control_posture": snapshot.control_posture,
        "policy_tier": snapshot.policy_tier,
        "topic_context": getattr(snapshot, "topic_context", ""),
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


# ---------------------------------------------------------------------------
# Session lifecycle (additive)
#
# ``/register``, ``/login`` and ``/me/password`` above are untouched: rotating
# refresh tokens and bulk logout are exposed as *new* routes so no existing
# client changes behaviour.
# ---------------------------------------------------------------------------


@router.post("/refresh", response_model=schemas.TokenRefreshResult)
async def refresh_session(payload: schemas.TokenRefreshRequest):
    """Exchange a refresh token for a fresh access token.

    Rotation is the default: the presented refresh token is revoked as it is
    presented, so a replayed one fails closed. Clients that need a reusable
    refresh token can opt out per request.
    """
    validation = security.decode_token_full(
        payload.refresh_token, expected_type=security.TOKEN_TYPE_REFRESH
    )
    if not validation.valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Refresh token rejected: {validation.reason}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    plan = build_refresh_plan(
        validation, rotate=payload.rotate_refresh_token, locale=payload.locale
    )
    result: dict[str, Any] = {
        "access_token": security.create_access_token(plan["data"]),
        "token_type": "bearer",
        "expires_in": plan["access_expires_in"],
        "rotated": plan["rotate"],
        "previous_jti_revoked": False,
        "locale": plan["locale"],
    }
    if plan["rotate"]:
        result["previous_jti_revoked"] = security.REVOCATIONS.revoke(
            plan["previous_jti"],
            subject=validation.subject,
            expires_at=plan["expires_at"],
        )
        result["refresh_token"] = security.create_refresh_token(plan["data"])
        result["refresh_expires_in"] = plan["refresh_expires_in"]
    return result


@router.post("/logout", response_model=schemas.LogoutResult)
async def logout(
    payload: schemas.LogoutRequest | None = None,
    credentials=Depends(deps.optional_security_scheme),
):
    """Revoke the presented credential, or every tracked session for the subject.

    Idempotent by design: logging out with an already-expired or undecodable
    token still succeeds, because from the caller's point of view they are done.
    """
    request = payload or schemas.LogoutRequest()
    token = (request.token or getattr(credentials, "credentials", None) or "").strip()

    if request.all_sessions:
        if not token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="all_sessions requires a token to identify the subject",
            )
        try:
            claims = security._decode_any(token)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        subject = claims.get("sub") or ""
        count = security.revoke_subject_tokens(subject)
        return {
            "revoked": True,
            "scope": "all_sessions",
            "newly_revoked": count,
            "token_type": claims.get("typ", security.TOKEN_TYPE_ACCESS),
            "message": f"Revoked {count} token(s) for {subject or 'the subject'}.",
        }

    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A token is required to log out",
        )
    report = security.revoke_token(token)
    return {
        "revoked": bool(report.get("revoked")),
        "scope": "token",
        "newly_revoked": 1 if report.get("newly_revoked") else 0,
        "token_type": report.get("token_type"),
        "message": "Logged out."
        if report.get("revoked")
        else "Token could not be decoded; treated as already logged out.",
    }


@router.get("/me/sessions", response_model=schemas.SessionInventoryOut)
async def list_my_sessions(
    credentials=Depends(deps.optional_security_scheme),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Inventory of the caller's session, including whether it is still live."""
    token = getattr(credentials, "credentials", None) or ""
    try:
        claims = security._decode_any(token)
    except ValueError:
        claims = {}
    subject = claims.get("sub") or current_user.username
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    if claims:
        rows.append(
            build_session_record(
                claims, revoked=security.REVOCATIONS.is_revoked(claims.get("jti"))
            )
        )
        seen.add(str(claims.get("jti") or ""))
    # Every jti already on the deny-list for this subject is a dead session.
    # The presented token is skipped so a revoked-and-presented credential is
    # listed once, not twice.
    for jti in security.REVOCATIONS.stats()["tracked_jtis"]:
        if jti in seen:
            continue
        rows.append(
            build_session_record(
                {
                    "jti": jti,
                    "sub": subject,
                    "typ": security.TOKEN_TYPE_REFRESH,
                },
                revoked=True,
            )
        )
    return build_session_inventory(subject, rows)


@router.get("/me/step-up", response_model=dict)
async def read_my_step_up(
    target_level: str = "loa2",
    principal: deps.Principal = Depends(deps.get_principal),
):
    """Explain what a step-up token would grant and which evidence qualifies."""
    plan = build_step_up_plan(target_level, current_level=principal.step_up_level)
    return {
        "subject": principal.subject,
        "auth_method": principal.auth_method,
        **plan,
    }


@router.post("/me/step-up", response_model=schemas.StepUpResult)
async def request_step_up(
    payload: schemas.StepUpRequest,
    current_user: models.User = Depends(deps.get_current_user),
):
    """Mint a step-up access token.

    The assurance level is bound into the ``acr``/``amr`` claims, so every
    downstream ``require_step_up`` check sees it without extra state.
    """
    plan = build_step_up_plan(payload.level, method=payload.method)
    if not plan["known_level"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown step-up level: {payload.level}",
        )
    if not plan["method_accepted"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Method '{payload.method}' does not satisfy {payload.level}; "
            f"expected one of {plan['accepted_methods']}",
        )
    data = security.with_step_up({"sub": current_user.username}, plan["requested_level"], [payload.method])
    return {
        "access_token": security.create_access_token(data),
        "token_type": "bearer",
        "expires_in": security.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "level": plan["requested_level"],
        "granted_level": plan["requested_level"],
        "satisfied": True,
        "methods": [payload.method],
    }


# ---------------------------------------------------------------------------
# Machine credentials (additive)
# ---------------------------------------------------------------------------


def _records_for(subject: str) -> list[security.ApiKeyRecord]:
    """Rehydrate registry rows for one subject (the registry keeps digests)."""
    rows: list[security.ApiKeyRecord] = []
    for row in security.API_KEYS.list_keys():
        if row["subject"] == subject:
            rows.append(
                security.ApiKeyRecord(
                    key_id=row["key_id"],
                    name=row["name"],
                    subject=row["subject"],
                    scopes=tuple(row["scopes"]),
                    hash_prefix=row["hash_prefix"],
                    expires_at=row["expires_at"],
                    created_at=row["created_at"],
                    revoked=row["revoked"],
                )
            )
    return rows


@router.get("/me/api-keys", response_model=schemas.ApiKeyListOut)
async def list_my_api_keys(current_user: models.User = Depends(deps.get_current_user)):
    """List the caller's machine credentials. Secrets are never returned."""
    return build_api_key_inventory(current_user.username, _records_for(current_user.username))


@router.post("/me/api-keys", response_model=schemas.ApiKeyOut)
async def create_my_api_key(
    payload: schemas.ApiKeyCreate,
    current_user: models.User = Depends(deps.get_current_user),
):
    """Mint a machine credential for the caller.

    Only self-service scopes are grantable — the wildcard is reserved for
    operator-provisioned keys.
    """
    granted, rejected = filter_self_scopes(payload.scopes or API_KEY_MISSING_SCOPES)
    if rejected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Scopes not available for self-service: {rejected}",
        )
    if payload.expires_in_days is not None and not 0 < payload.expires_in_days <= API_KEY_MAX_TTL_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"expires_in_days must be between 1 and {API_KEY_MAX_TTL_DAYS}",
        )
    secret, record = security.API_KEYS.issue(
        name=payload.name,
        subject=current_user.username,
        scopes=granted,
        expires_in_days=payload.expires_in_days,
    )
    return build_api_key_payload(record, secret)


@router.delete("/me/api-keys/{key_id}", response_model=schemas.ApiKeyRevokeResult)
async def revoke_my_api_key(
    key_id: str,
    current_user: models.User = Depends(deps.get_current_user),
):
    """Revoke one of the caller's own credentials.

    Ownership is checked before the registry is touched, so a caller cannot
    probe another subject's key ids.
    """
    owned = {record.key_id for record in _records_for(current_user.username)}
    if key_id not in owned:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )
    revoked = security.API_KEYS.revoke(key_id)
    return {
        "key_id": key_id,
        "revoked": revoked,
        "message": "API key revoked." if revoked else "API key was already revoked.",
    }


# ---------------------------------------------------------------------------
# Password feedback & posture (additive, non-mutating)
# ---------------------------------------------------------------------------


@router.get("/password-policy", response_model=schemas.PasswordPolicyOut)
async def read_password_policy():
    """Describe the active password policy without changing anything."""
    policy = security.DEFAULT_PASSWORD_POLICY
    return {
        "policy": security.password_policy_payload(policy),
        "history_depth": security.DEFAULT_PASSWORD_HISTORY.depth_configured(),
        "requirements": policy_requirements(policy),
        "advice": policy_advice(policy),
        "scale": [0, 4],
    }


@router.post("/me/password-feedback", response_model=schemas.PasswordFeedbackOut)
async def read_password_feedback(
    payload: schemas.PasswordFeedbackRequest,
    current_user: models.User = Depends(deps.get_current_user),
):
    """Score a candidate password without storing or changing anything.

    Safe to call from a client-side strength meter: it neither mutates the
    password history nor reveals whether a *previous* password matched, beyond
    the reuse flag the caller could learn anyway.
    """
    return build_password_feedback(
        payload.candidate,
        username=payload.username or current_user.username,
        history=security.DEFAULT_PASSWORD_HISTORY,
    )


@router.get("/me/security-posture", response_model=schemas.SecurityPostureOut)
async def read_my_security_posture(
    credentials=Depends(deps.optional_security_scheme),
    current_user: models.User = Depends(deps.get_current_user),
    principal: deps.Principal = Depends(deps.get_principal),
):
    """Join session, credential and assurance state for the caller."""
    token = getattr(credentials, "credentials", None) or ""
    try:
        claims = security._decode_any(token)
    except ValueError:
        claims = {}
    sessions = [
        {"claims": claims, "revoked": security.REVOCATIONS.is_revoked(claims.get("jti"))}
    ] if claims else []
    return build_security_posture(
        principal.subject or current_user.username,
        control_posture=_current_control_posture(current_user),
        step_up_level=principal.step_up_level,
        auth_method=principal.auth_method,
        sessions=sessions,
        keys=_records_for(current_user.username),
        is_admin=bool(getattr(current_user, "is_admin", False)),
    )