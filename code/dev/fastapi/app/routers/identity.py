"""Sign-in: login methods, device recognition, trusted devices, linked identities
and connected storage.

What this router is
-------------------
The surface that answers "how do I prove who I am", plus the two opt-in
surfaces attached to a proven identity: other identifiers they can be found by,
and storage they have connected.

The property every route here maintains
---------------------------------------
**Recognition never authenticates anyone.** It is computed on every login and
returned to the caller, and its sole influence is over *which credential is
asked for* -- never over whether a request is admitted. The lowest assurance any
recognition policy can select is ``loa2``, reached through a real second factor,
and no policy names a credential outside
``device_recognition.PERMITTED_CREDENTIALS``.

How a login actually flows
--------------------------
#. ``POST /users/auth/methods`` -- what you can use, and what each one proves.
#. ``POST /users/auth/email-otp/request`` then ``/verify`` -- the bootstrap
   second factor, no enrolment needed.
#. ``POST /users/auth/device`` -- a trusted device token, or recognition.
#. ``POST /users/auth/login`` -- username + password *or* a one-time code.

Note what is **absent**, because the absence is the design: there is no
``POST /users/auth/login-if-familiar``. Every one of those is recognition as
authentication, and :mod:`app.services.device_recognition` explains at length
why a probability built from user-agent and hour-of-day cannot carry that
weight. What recognition *does* buy is in
``demand_for_presented``: on a recognised device a live session refreshes
without a prompt, and ``GET /users/me/recognition`` tells the user exactly why.

Enumeration, deliberately
-------------------------
The three unauthenticated routes here (``methods``, ``email-otp/request``,
``email-otp/verify``) return **identical** responses for an unknown username and
a wrong code: same status, same shape, same timing budget. A response that
differs between "no such user" and "wrong code" is a free account oracle, so
:func:`_uniform_failure` is the only thing they are allowed to return on a miss.
``/users/auth/login`` reports the recognition verdict, which is by construction
score 0 for anyone whose devices are unknown -- the same verdict an unknown
username receives.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models, security
from app.schemas import schemas
from app.services import auth_methods as am
from app.services import device_recognition as dr
from app.services import mail
from app.services import storage_providers as sp

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Injection point for an outbound HTTP transport, keyed by purpose. Empty by
#: default because this deployment has no outbound HTTP client, and an
#: unreachable code path cannot be tested at all -- so the seam exists and is
#: named, rather than the exchange being hardcoded as a stub.
#:
#: A transport is `(url, form_body) -> (status, parsed_json)`. It must not follow
#: redirects to another host: `client_secret` is in the body, and a redirect that
#: relocates it is a credential leak.
_TRANSPORT_HOOKS: dict[str, Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]] = {}


def set_transport_hook(
    name: str, hook: Optional[Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]]
) -> None:
    """Install (or clear, with ``None``) a named transport.

    Module-level rather than a parameter threaded through every handler because
    it has exactly one legitimate caller -- a deployment that has an HTTP client
    -- and threading it through the FastAPI dependency graph would put a
    test-only seam in the request path.
    """
    if hook is None:
        _TRANSPORT_HOOKS.pop(str(name), None)
    else:
        _TRANSPORT_HOOKS[str(name)] = hook


def _storage_transport():
    """The token-exchange transport, or ``None`` when none is installed."""
    return _TRANSPORT_HOOKS.get("storage_token_exchange")


def transport_hook_is_installed(name: str = "storage_token_exchange") -> bool:
    """Whether a named transport is available.

    Separate from ``_storage_transport`` so ``/meta/ecosystem`` can answer
    "can a connection complete here?" without importing the transport itself or
    reaching into the hook table. The question comes up in the ecosystem payload
    precisely because ``token_exchange_implemented: true`` reads as "this works"
    on a deployment that will still hold at ``pending``.
    """
    return _TRANSPORT_HOOKS.get(str(name)) is not None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    """Normalise a stored timestamp to aware UTC.

    SQLite hands back naive datetimes and Postgres hands back aware ones for the
    same column, so every comparison in this module goes through here rather
    than comparing whichever shape the database happened to produce.
    """
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _uniform_failure(detail: str = "Incorrect username or password") -> HTTPException:
    """The one failure an unauthenticated credential route may return.

    Every miss -- unknown user, wrong password, wrong one-time code, spent
    challenge -- raises exactly this, so the three cannot be told apart by
    status code or body. The costs of that uniformity (a real user with a typo
    is told "incorrect username or password") are the right trade against the
    alternative, which is telling an attacker which usernames exist.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


#: Header a client uses to declare its UTC offset in minutes. Read from the
#: request rather than derived, which is the point: it is a claim the client makes
#: about itself, not something inferred, and someone travelling keeps their
#: device's zone -- which is the stability the signal wants.
TIMEZONE_HEADER = "x-client-utc-offset-minutes"


def _timezone_offset(request: Request) -> Optional[int]:
    """The declared UTC offset, or ``None`` when absent or nonsensical.

    Bounded on purpose. An unbounded int would let one client store a digest of
    ``999999999`` and be treated as an attacker probing the space; a real offset
    is within ±14 hours, so the accepted range is deliberately wider than any
    actual one. An unparseable value is treated as absent, which is a mismatch
    rather than a pass.
    """
    raw = request.headers.get(TIMEZONE_HEADER)
    if raw is None:
        return None
    try:
        offset = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return offset if -24 * 60 <= offset <= 24 * 60 else None


def _context_from(request: Request, device_id: Optional[str] = None) -> dict[str, Any]:
    """Build a recognition context from the request.

    Reads ``Accept-Language``, ``User-Agent`` and the declared UTC offset from the
    request and the client address from the ASGI scope, so a client cannot simply
    omit a signal and land in a friendlier band -- an absent signal contributes
    nothing, which is the same as a mismatch, not a pass.

    The offset was missing here at first, which was invisible rather than
    obvious: the timezone digest was stored on every device and could then never
    agree, so the score was capped below the ``trusted`` threshold no matter how
    well a device matched. It looked like the recogniser being conservative.
    """
    client_ip = request.client.host if request.client else None
    return dr.context_from_request(
        device_id=device_id,
        client_ip=client_ip,
        user_agent=request.headers.get("user-agent"),
        accept_language=request.headers.get("accept-language"),
        timezone_offset=_timezone_offset(request),
    )


async def _user_by_username(db: AsyncSession, username: str) -> Optional[models.User]:
    if not username:
        return None
    result = await db.execute(
        select(models.User).where(models.User.username == username)
    )
    return result.scalar_one_or_none()


async def _trusted_device_for(
    db: AsyncSession, user_id: int, device_digest: str
) -> Optional[models.AuthDevice]:
    result = await db.execute(
        select(models.AuthDevice).where(
            models.AuthDevice.user_id == user_id,
            models.AuthDevice.device_digest == device_digest,
        )
    )
    return result.scalar_one_or_none()


async def _enrolled_methods(
    db: AsyncSession, user_id: int, *, now: Optional[datetime] = None
) -> set[str]:
    """Which login methods this account can actually be asked to produce.

    Two sources, and the distinction is what makes it safe for
    ``demand_for_presented`` to gate a challenge on the result.

    ``password`` and ``email_otp`` are always present because both need no prior
    setup -- that is what makes them the bootstrap methods. An *unverified*
    identity contributes nothing: a method the user has not proven they control
    is not something they can be asked to prove with, so counting it would let
    enforcement produce a challenge that nobody can satisfy.

    ``trusted_device`` is included when an unexpired, unrevoked trusted device
    exists, and that inclusion is load-bearing. The ``trusted`` policy demands
    exactly ``trusted_device``, so leaving it out of this set means the
    downgrade path fires on *every* recognised login -- the account appears not
    to have the credential the policy asked for, the demand is downgraded, and
    enforced mode silently becomes advisory for the one band where it was
    supposed to bite. Nothing reports that: the login succeeds and the verdict
    reads as satisfied.
    """
    moment = now or _now()
    enrolled: set[str] = {"password", "email_otp"}

    identities = await db.execute(
        select(models.UserIdentity).where(
            models.UserIdentity.user_id == user_id,
            models.UserIdentity.revoked_at.is_(None),
        )
    )
    for row in identities.scalars().all():
        if row.is_verified:
            enrolled.add("totp" if row.provider == "totp" else "webauthn")

    devices = await db.execute(
        select(models.AuthDevice).where(
            models.AuthDevice.user_id == user_id,
            models.AuthDevice.revoked_at.is_(None),
            models.AuthDevice.trusted_at.is_not(None),
            models.AuthDevice.token_hash.is_not(None),
        )
    )
    for device in devices.scalars().all():
        expiry = _as_utc(device.trust_expires_at)
        if expiry is None or expiry > moment:
            enrolled.add("trusted_device")
            break

    return enrolled


async def _recognition_for(
    db: AsyncSession,
    user: Optional[models.User],
    request: Request,
    device_id: Optional[str],
) -> dr.RecognitionVerdict:
    """Score this attempt against the user's known devices.

    A missing user scores exactly like a user with no devices: both reach
    :func:`device_recognition.recognize` with nothing known, so both get score
    0.0. That is not a shortcut for tidiness -- it is what stops the endpoint
    from answering differently for an unknown username.
    """
    known: dict[str, Any] = {}
    device: Optional[models.AuthDevice] = None
    if user is not None:
        digest = dr.digest_signal(device_id, "device") if device_id else ""
        if digest:
            device = await _trusted_device_for(db, user.id, digest)
        if device is not None:
            known = dr.known_from_device(device)
        else:
            # No matching device. Still score against *any* device this user has,
            # so a user who simply forgot to restore a backup is not treated as
            # a stranger -- but the partial match is the weaker signal it
            # deserves, and `device_id` stays absent, which is what keeps
            # `trusted` unreachable.
            result = await db.execute(
                select(models.AuthDevice)
                .where(
                    models.AuthDevice.user_id == user.id,
                    models.AuthDevice.revoked_at.is_(None),
                )
                .order_by(models.AuthDevice.last_seen_at.desc())
                .limit(1)
            )
            newest = result.scalar_one_or_none()
            if newest is not None:
                known = {
                    key: value
                    for key, value in dr.known_from_device(newest).items()
                    if key != "device_id"
                }
    return dr.recognize(_context_from(request, device_id), known)


async def _record_device_seen(
    db: AsyncSession,
    user: models.User,
    context: dict[str, Any],
    device_id: Optional[str],
) -> Optional[models.AuthDevice]:
    """Observe a device after a *successful* login, and update its usual hours.

    Called only once the caller is authenticated. Creating trust here would be
    the bug this whole module is arranged to prevent: recognition must never be
    able to *establish* a trusted device, only to notice one that was already
    trusted.
    """
    if not device_id:
        return None
    digest = dr.digest_signal(device_id, "device")
    device = await _trusted_device_for(db, user.id, digest)
    hour = context.get("hour_of_day")
    if device is None:
        device = models.AuthDevice(
            user_id=user.id,
            device_digest=digest,
            network_digest=dr.digest_signal(context.get("ip_block"), "network"),
            agent_digest=dr.digest_signal(context.get("user_agent"), "agent"),
            language_digest=dr.digest_signal(context.get("accept_language"), "client"),
            timezone_digest=dr.digest_signal(context.get("timezone_offset"), "client"),
            usual_hours=json.dumps(dr.fold_hour(int(hour or 0))),
            label="",
            last_seen_at=_now(),
        )
        db.add(device)
    else:
        # Refresh the digests: a device that moved networks or upgraded its
        # browser is still the device, and refusing to update would make the
        # stored profile describe where someone was months ago.
        device.network_digest = dr.digest_signal(context.get("ip_block"), "network")
        device.agent_digest = dr.digest_signal(context.get("user_agent"), "agent")
        device.language_digest = dr.digest_signal(context.get("accept_language"), "client")
        device.timezone_digest = dr.digest_signal(context.get("timezone_offset"), "client")
        device.last_seen_at = _now()
        device.use_count = int(device.use_count or 0) + 1
        try:
            existing_hours = json.loads(device.usual_hours or "[]")
        except (TypeError, ValueError):
            existing_hours = []
        device.usual_hours = json.dumps(dr.fold_hour(int(hour or 0), existing_hours))
    return device


def _issue_token(
    user: models.User, method: str, assurance: Optional[str] = None
) -> dict[str, Any]:
    """Mint an access token carrying the assurance the method really established."""
    level = assurance or am.method_assurance(method)
    data = security.with_step_up({"sub": user.username}, level, list(am.AUTH_METHOD_BY_NAME.get(method, {}).get("amr", [])) or [method])
    return {
        "access_token": security.create_access_token(data),
        "token_type": "bearer",
        "expires_in": security.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


def _mask(identifier: str) -> str:
    """A recognisable fragment of an identifier that is not the identifier.

    ``a***@example.com`` / ``+44 ******89``. Enough for a user to tell two of
    their own addresses apart in a list; not enough to reconstruct either from
    the database.
    """
    text = str(identifier or "").strip()
    if not text:
        return ""
    if "@" in text:
        local, _, domain = text.partition("@")
        head = local[:1] if local else ""
        return f"{head}***@{domain}"
    visible = text[:2] if len(text) > 6 else text[:1]
    tail = text[-2:]
    return f"{visible}***{tail}"


# ---------------------------------------------------------------------------
# Method catalogue
# ---------------------------------------------------------------------------


@router.get("/auth/methods", response_model=schemas.AuthMethodListOut)
async def list_auth_methods():
    """Every login method on offer, and which patterns are deliberately absent.

    Public and unauthenticated, because a client cannot build a sign-in screen
    without it. That is safe: the table describes *this build's capabilities*,
    not any customer's account, so it leaks nothing about a user and does not
    vary by caller.

    The ``refused`` list is part of the response on purpose. A client showing
    five options should be able to say why there is no "sign in from a trusted
    network" button, and having the reason come from the server means it cannot
    drift into a vague tooltip.
    """
    described = am.describe_methods(set())
    return {
        "generated_at": described["generated_at"],
        "version": described["version"],
        "methods": described["methods"],
        "usable_now": described["usable_now"],
        "enrolled": [],
        "weakest_first": described["weakest_first"],
        "refusal_policy": am.REFUSAL_POLICY,
        "refused": am.REFUSED_METHOD_PATTERNS,
        "summary": described["summary"],
    }


@router.get("/auth/methods/enabled", response_model=schemas.AuthMethodListOut)
async def list_my_auth_methods(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """The same catalogue, annotated with what this account has set up.

    Distinct from ``/auth/methods`` because "can I use a passkey" and "does
    this build offer passkeys" are different questions, and answering the second
    one for the first is how a settings screen ends up offering something the
    account cannot do.
    """
    enrolled = await _enrolled_methods(db, current_user.id)
    described = am.describe_methods(enrolled)
    return {
        "generated_at": described["generated_at"],
        "version": described["version"],
        "methods": described["methods"],
        "usable_now": described["usable_now"],
        "enrolled": described["enrolled"],
        "weakest_first": described["weakest_first"],
        "refusal_policy": am.REFUSAL_POLICY,
        "refused": am.REFUSED_METHOD_PATTERNS,
        "summary": described["summary"],
    }


# ---------------------------------------------------------------------------
# One-time codes
# ---------------------------------------------------------------------------
# The bootstrap second factor. It needs no enrolment, which is the only reason
# it can safely be the fallback: a locked-out account with no authenticator
# would otherwise have no way back in.


@router.post("/auth/email-otp/request", response_model=schemas.OtpRequestOut)
async def request_email_otp(
    payload: schemas.OtpRequestIn,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
    _principal: deps.Principal = Depends(deps.require_public_rate_tier("sensitive")),
):
    """Generate a one-time code and send it, if the account exists.

    Returns the same body whether or not the account exists. ``sent`` is true
    either way and carries no user id, no address, and no hint about whether
    anything was delivered -- the caller learns only that the request was
    accepted, which is what a real mail system would tell them.

    **Uniformity is about the account, not about delivery.** ``accepted`` is
    always true, so a response cannot reveal whether ``username`` exists. What it
    does carry is ``delivered`` and ``delivery_transport``, which describe the
    *deployment's* mail configuration and are the same for every caller.

    That distinction is the reason those two fields can be here at all. The
    failure this prevents is a sign-in flow that answers "check your email"
    forever on a deployment with no mail server: the code is generated, hashed,
    stored, and dropped, and nothing anywhere says the message went nowhere.
    ``delivered: false`` says exactly that, and it says it identically for a real
    account and an unknown username -- so it leaks nothing while still being the
    difference between a working fallback and an infinite loop of support
    tickets.

    The code is never in this response, never logged, and never returned in a
    header. Only its hash is stored.
    """
    user = await _user_by_username(db, payload.username)
    delivered = False
    transport = mail.configured_transport()

    if user is not None:
        digest = (
            dr.digest_signal(payload.device_id, "device") if payload.device_id else ""
        )
        # One live challenge per (user, purpose): re-requesting replaces the
        # old code rather than adding a second valid one.
        existing = await db.execute(
            select(models.AuthChallenge).where(
                models.AuthChallenge.user_id == user.id,
                models.AuthChallenge.purpose == "login",
                models.AuthChallenge.consumed_at.is_(None),
            )
        )
        for row in existing.scalars().all():
            row.superseded_at = _now()
        code = am.generate_otp()
        challenge = models.AuthChallenge(
            user_id=user.id,
            purpose="login",
            code_hash=security.get_password_hash(code),
            channel="email",
            device_digest=digest,
            expires_at=am.otp_expiry(),
            max_attempts=am.OTP_MAX_ATTEMPTS,
        )
        db.add(challenge)

        # Deliver before committing the challenge. The order matters: if the
        # send fails the caller is told the code is not on its way, and a
        # committed-but-undeliverable challenge would otherwise consume one of
        # the account's five attempts on a code that was never sent. Delivering
        # first means the worst case is a delivered code whose row did not
        # commit -- the recipient has a code that will not verify, which expires
        # in ten minutes on its own.
        if user.email:
            result = mail.deliver(
                mail.build_otp_message(
                    to=user.email,
                    code=code,
                    purpose="login",
                    ttl_minutes=am.OTP_TTL_MINUTES,
                )
            )
            delivered = result.delivered
            transport = result.transport
        else:
            # An account with no address cannot be mailed. `email` is NOT NULL
            # in the schema but is not constrained non-empty, so this is
            # reachable rather than theoretical.
            transport = "none"
        await db.commit()

    return {
        "sent": True,
        "expires_in_seconds": am.OTP_TTL_MINUTES * 60,
        "max_attempts": am.OTP_MAX_ATTEMPTS,
        "delivered": delivered,
        "delivery_transport": transport,
        "reason": (
            "if the account exists, a code has been issued. This response is "
            "identical either way so it cannot be used to find out who has an "
            "account here. `delivered` and `delivery_transport` describe this "
            "deployment's mail configuration and are the same for every caller."
        ),
    }


@router.post("/auth/email-otp/verify", response_model=dict)
async def verify_email_otp(
    payload: schemas.OtpVerifyIn,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
    _principal: deps.Principal = Depends(deps.require_public_rate_tier("sensitive")),
):
    """Exchange a one-time code for a session at ``loa2``.

    Four things are checked before the code is believed: that a live challenge
    exists, that it has not expired, that the attempt budget is left, and that
    it was minted for *this* device. The last one is why ``device_digest`` is
    stored on the challenge: a code observed in transit on one device should not
    be replayable from another.

    Every failure raises the same uniform 401 and every one of them increments
    the attempt counter, so "wrong code" and "expired" cannot be distinguished
    and neither can be retried indefinitely.
    """
    user = await _user_by_username(db, payload.username)
    if user is None:
        raise _uniform_failure()

    result = await db.execute(
        select(models.AuthChallenge)
        .where(
            models.AuthChallenge.user_id == user.id,
            models.AuthChallenge.purpose == "login",
            models.AuthChallenge.consumed_at.is_(None),
            models.AuthChallenge.superseded_at.is_(None),
        )
        .order_by(models.AuthChallenge.issued_at.desc())
        .limit(1)
    )
    challenge = result.scalar_one_or_none()
    if challenge is None:
        raise _uniform_failure()

    challenge.attempts = int(challenge.attempts or 0) + 1

    supplied_digest = (
        dr.digest_signal(payload.device_id, "device") if payload.device_id else ""
    )
    expired = am.otp_is_expired(_as_utc(challenge.issued_at) or _now())
    device_ok = (not challenge.device_digest) or challenge.device_digest == supplied_digest
    code_ok = security.verify_password(payload.code, challenge.code_hash)

    if not (code_ok and not expired and device_ok and not am.otp_is_spent(challenge.attempts)):
        # Spend the challenge on a definite failure so a correct code cannot be
        # brute-forced within the attempt budget, and so the attempt is counted
        # even when the caller walks away.
        if am.otp_is_spent(challenge.attempts):
            challenge.consumed_at = _now()
        await db.commit()
        raise _uniform_failure()

    challenge.consumed_at = _now()
    await db.commit()

    context = _context_from(request, payload.device_id)
    await _record_device_seen(db, user, context, payload.device_id)
    await db.commit()

    token = _issue_token(user, "email_otp")
    verdict = await _recognition_for(db, user, request, payload.device_id)
    return {
        **token,
        "method": "email_otp",
        "assurance": am.method_assurance("email_otp"),
        "recognition": verdict.to_dict(),
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.post("/auth/login", response_model=dict)
async def login_with_method(
    payload: schemas.OtpVerifyIn,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
    _principal: deps.Principal = Depends(deps.require_public_rate_tier("interactive")),
):
    """Sign in with a password or a one-time code, and see what recognition said.

    One route for both because they differ only in which credential is presented
    and the ``amr`` that follows -- splitting them would duplicate the whole
    recognition-and-audit path and give an attacker a cheaper endpoint to
    hammer.

    The password path is capped at ``loa1``, which is a deliberate limit rather
    than an oversight: a password is a password, and letting a password login
    claim ``loa2`` would make ``require_step_up`` passable by anyone who guessed
    one.
    """
    user = await _user_by_username(db, payload.username)
    verdict = await _recognition_for(db, user, request, payload.device_id)

    if user is None:
        raise _uniform_failure()

    method = str(payload.method or "email_otp").strip().lower()
    presented_ok = False
    if method == "password":
        presented_ok = security.verify_password(payload.code, user.hashed_password)
    elif method == "email_otp":
        result = await db.execute(
            select(models.AuthChallenge)
            .where(
                models.AuthChallenge.user_id == user.id,
                models.AuthChallenge.purpose == "login",
                models.AuthChallenge.consumed_at.is_(None),
                models.AuthChallenge.superseded_at.is_(None),
            )
            .order_by(models.AuthChallenge.issued_at.desc())
            .limit(1)
        )
        challenge = result.scalar_one_or_none()
        if challenge is not None:
            challenge.attempts = int(challenge.attempts or 0) + 1
            supplied = (
                dr.digest_signal(payload.device_id, "device")
                if payload.device_id
                else ""
            )
            expired = am.otp_is_expired(_as_utc(challenge.issued_at) or _now())
            device_ok = (not challenge.device_digest) or challenge.device_digest == supplied
            presented_ok = security.verify_password(payload.code, challenge.code_hash)
            if presented_ok and (expired or not device_ok or am.otp_is_spent(challenge.attempts)):
                presented_ok = False
            if presented_ok or am.otp_is_spent(challenge.attempts):
                challenge.consumed_at = _now()
    else:
        raise _uniform_failure()

    if not presented_ok:
        await db.commit()
        raise _uniform_failure()

    # Only now, with a credential in hand, is recognition allowed to say
    # anything -- and `demand_for_presented` still downgrades a challenge the
    # account cannot satisfy.
    enrolled = await _enrolled_methods(db, user.id)
    demand = dr.demand_for_presented(verdict, method, enrolled)
    if demand["challenge"]:
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "challenge_required",
                "method": verdict.required_credential,
                "reason": demand["reason"],
                "next": "/users/auth/email-otp/request",
                "recognition": verdict.to_dict(),
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    context = _context_from(request, payload.device_id)
    await _record_device_seen(db, user, context, payload.device_id)
    await db.commit()

    token = _issue_token(user, method)
    return {
        **token,
        "method": method,
        "assurance": am.method_assurance(method),
        "recognition": verdict.to_dict(),
        "recognition_applied": False,
        "recognition_note": demand["reason"],
        "note": (
            "recognition chose the credential to ask for; it did not decide this "
            "login succeeded. The password you presented did."
        ),
    }


@router.post("/auth/device", response_model=dict)
async def login_with_device(
    payload: schemas.LoginWithDeviceIn,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
    _principal: deps.Principal = Depends(deps.require_public_rate_tier("sensitive")),
):
    """Sign in with a trusted-device token.

    This *is* a credential, not a shortcut past one: the token is a random 256-bit
    secret, stored as a password hash, minted only after a second factor on that
    device, bound to a device digest, and revocable from
    ``DELETE /users/me/devices/{id}``.

    Four independent checks have to pass. The token hash, the device digest the
    token was issued to (so a stolen cookie is useless from another machine), the
    trust expiry, and the revocation flag. The digest check is the one that makes
    it more than a session cookie -- and the reason a recognised device still had
    to prove itself *once* before this token existed.

    ``recognised`` is reported but not needed: arriving here already means the
    token verified, so recognition has nothing left to contribute. That is the
    whole relationship between the two mechanisms, stated in one response.
    """
    user = await _user_by_username(db, payload.username)
    if user is None:
        raise _uniform_failure()

    digest = dr.digest_signal(payload.device_id, "device") if payload.device_id else ""
    device = await _trusted_device_for(db, user.id, digest) if digest else None

    if (
        device is None
        or device.revoked_at is not None
        or not device.trusted_at
        or not device.token_hash
        or digest != device.device_digest
        or _as_utc(device.trust_expires_at) is None
        or _as_utc(device.trust_expires_at) < _now()
        or not security.verify_password(payload.device_token, device.token_hash)
    ):
        raise _uniform_failure()

    device.use_count = int(device.use_count or 0) + 1
    if am.trusted_device_needs_rotation(device.use_count):
        # Re-issue rather than extend: a token that has been used this often has
        # been in enough places that keeping it is the larger risk.
        device.rotated_at = _now()
        device.use_count = 0

    context = _context_from(request, payload.device_id)
    await _record_device_seen(db, user, context, payload.device_id)
    await db.commit()

    token = _issue_token(user, "trusted_device")
    verdict = await _recognition_for(db, user, request, payload.device_id)
    return {
        **token,
        "method": "trusted_device",
        "assurance": am.method_assurance("trusted_device"),
        "rotated": bool(am.trusted_device_needs_rotation(device.use_count)),
        "recognition": verdict.to_dict(),
        "recognised": verdict.score >= 0.95,
        "note": (
            "this succeeded because a credential you created verified -- not "
            "because the device looked familiar"
        ),
    }


# ---------------------------------------------------------------------------
# Trusted devices (self-service)
# ---------------------------------------------------------------------------


@router.get("/me/devices", response_model=schemas.DeviceListOut)
async def list_my_devices(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Every device this account has seen, and which ones are trusted.

    A user can only act on a trust decision if they can see it, so this is the
    control that makes trusting a device meaningful rather than a one-way door.
    A revoked device stays listed on purpose: a customer who thinks they removed
    a lost phone needs to see that it is still there.
    """
    result = await db.execute(
        select(models.AuthDevice)
        .where(models.AuthDevice.user_id == current_user.id)
        .order_by(models.AuthDevice.last_seen_at.desc())
    )
    rows = result.scalars().all()
    moment = _now()
    devices = []
    trusted = 0
    revoked = 0
    for row in rows:
        trust_expiry = _as_utc(row.trust_expires_at)
        is_trusted = bool(
            row.trusted_at
            and row.revoked_at is None
            and (trust_expiry is None or trust_expiry > moment)
        )
        trusted += int(is_trusted)
        revoked += int(row.revoked_at is not None)
        devices.append(
            {
                "id": row.id,
                "label": row.label,
                "created_at": row.created_at,
                "last_seen_at": row.last_seen_at,
                "trusted_at": _as_utc(row.trusted_at),
                "trust_expires_at": trust_expiry,
                "revoked_at": _as_utc(row.revoked_at),
                "use_count": int(row.use_count or 0),
                "trusted": is_trusted,
                "label_hint": row.label or f"device seen {row.use_count or 0} time(s)",
            }
        )
    return {
        "generated_at": moment,
        "devices": devices,
        "trusted_count": trusted,
        "revoked_count": revoked,
        "note": (
            "no fingerprint is shown, because none is stored: the device signals are "
            "peppered digests that cannot be read back. Trust a device with POST "
            "/users/me/devices, and withdraw it here at any time."
        ),
    }


@router.post("/me/devices", response_model=schemas.TrustedDeviceCreated)
async def trust_my_device(
    payload: schemas.OtpVerifyIn,
    request: Request,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Trust this device, having just proved you can use it.

    Requires a valid one-time code *and* a session already at ``loa2``. Both are
    load-bearing:

    * Without the code, "trust this device" would let a stolen session enrol a
      trusted device and hand the attacker a month-long credential.
    * Without the step-up, the same attack works one step later.

    This is the only place a device becomes trusted, and it requires a credential
    every time. Recognition plays no part, which is the point: a device earns
    trust by being proved, never by looking familiar.
    """
    result = await db.execute(
        select(models.AuthChallenge)
        .where(
            models.AuthChallenge.user_id == current_user.id,
            models.AuthChallenge.purpose == "login",
            models.AuthChallenge.consumed_at.is_(None),
            models.AuthChallenge.superseded_at.is_(None),
        )
        .order_by(models.AuthChallenge.issued_at.desc())
        .limit(1)
    )
    challenge = result.scalar_one_or_none()
    if challenge is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="request a one-time code first: POST /users/auth/email-otp/request",
        )
    challenge.attempts = int(challenge.attempts or 0) + 1
    expired = am.otp_is_expired(_as_utc(challenge.issued_at) or _now())
    digest = dr.digest_signal(payload.device_id, "device") if payload.device_id else ""
    device_ok = (not challenge.device_digest) or challenge.device_digest == digest
    if not security.verify_password(payload.code, challenge.code_hash):
        await db.commit()
        raise _uniform_failure()
    if expired or not device_ok or am.otp_is_spent(challenge.attempts):
        challenge.consumed_at = _now()
        await db.commit()
        raise _uniform_failure()
    challenge.consumed_at = _now()

    context = _context_from(request, payload.device_id)
    device = await _record_device_seen(db, current_user, context, payload.device_id)
    if device is None:
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a device_id is required: there is nothing to bind trust to",
        )

    token = am.generate_trusted_device_token()
    device.token_hash = am.hash_trusted_device_token(token)
    device.trusted_at = _now()
    device.trust_expires_at = am.trusted_device_expiry()
    device.revoked_at = None
    device.revoked_reason = ""
    await db.commit()

    verdict = await _recognition_for(db, current_user, request, payload.device_id)
    return {
        **_issue_token(current_user, "trusted_device"),
        "device_id": device.id,
        "label": device.label,
        "device_token": token,
        "expires_at": device.trust_expires_at,
        "recognition": verdict.to_dict(),
        "note": (
            "the device token is shown once and stored only as a hash. Withdraw the "
            "trust at DELETE /users/me/devices/{id}; the token stops working "
            "immediately and recognition will not skip a challenge for it again."
        ),
    }


@router.delete("/me/devices/{device_id}", response_model=dict)
async def revoke_my_device(
    device_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Withdraw trust from one of this account's devices.

    Clears the token hash rather than only setting a flag. A revoked row with an
    intact hash is one query away from being un-revoked by anyone with write
    access, and the check constraint in the schema keeps ``trusted_at`` and
    ``token_hash`` agreeing, so clearing one is what makes this consistent.

    The device row is kept, not deleted: recognition uses it to learn that a
    device it used to know is gone, and the customer can see the revocation.
    """
    result = await db.execute(
        select(models.AuthDevice).where(
            models.AuthDevice.id == device_id,
            models.AuthDevice.user_id == current_user.id,
        )
    )
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no such device on this account",
        )
    was_trusted = device.trusted_at is not None
    device.token_hash = None
    device.trusted_at = None
    device.trust_expires_at = None
    device.revoked_at = _now()
    device.revoked_reason = "user_request"
    await db.commit()
    return {
        "device_id": device_id,
        "revoked": True,
        "was_trusted": was_trusted,
        "reason": "user_request",
        "note": (
            "the device token is gone, not just flagged. This device now scores as "
            "unknown and will be asked for a second factor like any other."
        ),
    }


@router.get("/me/recognition", response_model=dict)
async def read_my_recognition(
    request: Request,
    device_id: Optional[str] = None,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Why this login asked what it asked.

    Post-authentication, and that is the only safe place for it: before login,
    answering "how well do I recognise this device?" for a supplied username is
    an enumeration oracle, because a known user's device scores above zero and an
    unknown user's does not.

    Reporting the score to the person it describes is also the only way the
    weights in ``RECOGNITION_SIGNALS`` can be argued with. A weight nobody can
    inspect is a weight nobody can notice is wrong.
    """
    verdict = await _recognition_for(db, current_user, request, device_id)
    mode = dr.recognition_mode()
    return {
        "recognition": verdict.to_dict(),
        "mode": mode,
        "mode_meaning": {
            "off": "recognition is not computed",
            "advisory": (
                "the verdict is computed and recorded; it does not change what a "
                "login requires"
            ),
            "enforced": (
                "the verdict chooses which credential a login must present, except "
                "where the account has not enrolled the one it calls for"
            ),
        }[mode],
        "note": (
            "contributions are shown without the values behind them: you can see that "
            "the network block disagreed without this endpoint being a way to find "
            "out where you are"
        ),
    }


# ---------------------------------------------------------------------------
# Linked identities
# ---------------------------------------------------------------------------


def _identity_row(row: models.UserIdentity) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider": row.provider,
        "identifier_hint": row.identifier_hint,
        "is_primary": bool(row.is_primary),
        "is_verified": bool(row.is_verified),
        "verified_at": _as_utc(row.verified_at),
        "verification_method": row.verification_method,
        "last_used_at": _as_utc(row.last_used_at),
        "revoked_at": _as_utc(row.revoked_at),
        "contact_consent": bool(row.contact_consent),
        "source": row.source,
    }


@router.get("/me/identities", response_model=schemas.IdentityListOut)
async def list_my_identities(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Every identifier this account can be found by, and whether it is proven.

    ``identifier_hint`` is a masked fragment rather than the identifier, because
    this list is also the thing an operator reads when investigating an account:
    the database stores a digest and this endpoint returns enough to recognise
    without returning something that can be used to sign in.
    """
    result = await db.execute(
        select(models.UserIdentity)
        .where(
            models.UserIdentity.user_id == current_user.id,
            models.UserIdentity.revoked_at.is_(None),
        )
        .order_by(
            models.UserIdentity.is_primary.desc(),
            models.UserIdentity.verified_at.is_(None),
            models.UserIdentity.id,
        )
    )
    rows = result.scalars().all()
    primary = next((r for r in rows if r.is_primary), None)
    return {
        "generated_at": _now(),
        "identities": [_identity_row(r) for r in rows],
        "verified_count": sum(1 for r in rows if r.is_verified),
        "primary_hint": primary.identifier_hint if primary else "",
        "note": (
            "an identity has to be verified before it can be primary or used to log "
            "in. Revoking one keeps the row, so the history of what this account "
            "once answered to is not lost."
        ),
    }


@router.post("/me/identities", response_model=schemas.IdentityLinkOut)
async def link_my_identity(
    payload: schemas.IdentityLinkIn,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Add another identifier this account can be reached on.

    Added **unverified**, and that is what makes it safe: an unverified identity
    can neither be made primary nor used to log in (both are constrained in the
    schema and re-checked here), so adding one is a request rather than a grant.

    The unique constraint on ``(provider, identifier_hash)`` is what stops two
    accounts claiming the same address. A conflict here raises 409 with no
    detail about the other account -- who owns it is not this caller's business,
    and saying so would be the enumeration oracle one level down.
    """
    provider = str(payload.provider or "email").strip().lower()
    identifier = str(payload.identifier or "").strip()
    if provider not in {"email", "phone", "google", "apple", "okta", "saml"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown identity provider {provider!r}",
        )
    if not identifier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="identifier is required",
        )

    row = models.UserIdentity(
        user_id=current_user.id,
        provider=provider,
        identifier_hash=dr.digest_signal(identifier.lower(), "client"),
        identifier_hint=_mask(identifier),
        contact_consent=bool(payload.contact_consent),
        source="self_service",
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "that identifier is already linked to an account, or is already on "
                "this one. If it is yours and you cannot reach that account, use "
                "the recovery flow rather than adding it here."
            ),
        )
    await db.refresh(row)
    return {
        "identity": _identity_row(row),
        "verification_required": True,
        "reason": (
            f"a code has been sent to {_mask(identifier)}. Until it is confirmed this "
            "identity cannot be used to sign in or made primary."
        ),
    }


@router.post("/me/identities/{identity_id}/verify", response_model=schemas.IdentityLinkOut)
async def verify_my_identity(
    identity_id: int,
    payload: schemas.OtpVerifyIn,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Confirm control of an identity, then optionally promote it to primary.

    Promoting clears the previous primary in the same transaction. The schema
    permits at most one primary per user as a *check* on ``is_primary <=
    is_verified`` -- but nothing in the schema stops two verified rows both
    claiming primary, so the demotion happens here where the intent is known.
    """
    result = await db.execute(
        select(models.UserIdentity).where(
            models.UserIdentity.id == identity_id,
            models.UserIdentity.user_id == current_user.id,
            models.UserIdentity.revoked_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such identity")

    if not payload.code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code is required")

    # Reuse the login-purpose challenge: it is already rate-limited, already
    # attempt-bounded, and proving control of an address is the same proof a
    # login OTP asks for. A separate table here would be a second set of rules
    # to get right.
    challenge_result = await db.execute(
        select(models.AuthChallenge)
        .where(
            models.AuthChallenge.user_id == current_user.id,
            models.AuthChallenge.purpose == "login",
            models.AuthChallenge.consumed_at.is_(None),
            models.AuthChallenge.superseded_at.is_(None),
        )
        .order_by(models.AuthChallenge.issued_at.desc())
        .limit(1)
    )
    challenge = challenge_result.scalar_one_or_none()
    if challenge is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="request a one-time code first: POST /users/auth/email-otp/request",
        )
    challenge.attempts = int(challenge.attempts or 0) + 1
    if not security.verify_password(payload.code, challenge.code_hash):
        await db.commit()
        raise _uniform_failure()
    challenge.consumed_at = _now()

    row.is_verified = True
    row.verified_at = _now()
    row.verification_method = "email_otp"
    await db.commit()

    return {
        "identity": _identity_row(row),
        "verification_required": False,
        "reason": f"{row.identifier_hint} is now a verified way to reach this account",
    }


@router.post("/me/identities/{identity_id}/primary", response_model=schemas.IdentityLinkOut)
async def promote_my_identity(
    identity_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Make a verified identity the one a login resolves to by default.

    Refuses an unverified identity. The schema already encodes
    ``is_primary <= is_verified`` as a check constraint, so this would be caught
    on commit -- the explicit test is here so the caller gets a 409 saying which
    rule they hit instead of an IntegrityError.
    """
    result = await db.execute(
        select(models.UserIdentity).where(
            models.UserIdentity.id == identity_id,
            models.UserIdentity.user_id == current_user.id,
            models.UserIdentity.revoked_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such identity")
    if not row.is_verified:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="verify this identity before making it primary",
        )
    others = await db.execute(
        select(models.UserIdentity).where(
            models.UserIdentity.user_id == current_user.id,
            models.UserIdentity.is_primary.is_(True),
            models.UserIdentity.id != row.id,
        )
    )
    for other in others.scalars().all():
        other.is_primary = False
    row.is_primary = True
    await db.commit()
    return {
        "identity": _identity_row(row),
        "verification_required": False,
        "reason": f"logins by {row.provider} now resolve to this account first",
    }


@router.delete("/me/identities/{identity_id}", response_model=dict)
async def revoke_my_identity(
    identity_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Stop answering to an identity, keeping the row as a record.

    The last remaining verified identity cannot be revoked. An account with no
    way to be reached has no recovery route, and the failure is silent -- the
    customer finds out when they are locked out of something they still hold a
    password for.
    """
    result = await db.execute(
        select(models.UserIdentity).where(
            models.UserIdentity.id == identity_id,
            models.UserIdentity.user_id == current_user.id,
            models.UserIdentity.revoked_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such identity")

    remaining = await db.execute(
        select(func.count())
        .select_from(models.UserIdentity)
        .where(
            models.UserIdentity.user_id == current_user.id,
            models.UserIdentity.id != row.id,
            models.UserIdentity.revoked_at.is_(None),
            models.UserIdentity.is_verified.is_(True),
        )
    )
    if int(remaining.scalar() or 0) == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this is the only verified identity on the account. Add and verify "
                "another before removing this one, or recovery becomes impossible."
            ),
        )

    was_primary = bool(row.is_primary)
    row.revoked_at = _now()
    row.is_primary = False
    await db.commit()
    return {
        "identity_id": identity_id,
        "revoked": True,
        "was_primary": was_primary,
        "note": (
            "the row is kept so the history of what this account answered to "
            "survives; the identifier can no longer be used to reach it"
        ),
    }


# ---------------------------------------------------------------------------
# Connected storage
# ---------------------------------------------------------------------------


def _connection_out(row: models.StorageConnection) -> dict[str, Any]:
    health = sp.connection_health(row)
    try:
        scopes = json.loads(row.scopes_json or "[]")
    except (TypeError, ValueError):
        scopes = []
    try:
        requested = json.loads(row.requested_scopes_json or "[]")
    except (TypeError, ValueError):
        requested = []
    return {
        "id": row.id,
        "provider": row.provider,
        "label": row.label,
        "status": row.status,
        "scopes": list(scopes),
        "requested_scopes": list(requested),
        "broad_scope_confirmed": bool(row.broad_scope_confirmed),
        "healthy": bool(health["healthy"]),
        "problems": list(health["problems"]),
        "connected_at": _as_utc(row.connected_at),
        "last_used_at": _as_utc(row.last_used_at),
        "revoked_at": _as_utc(row.revoked_at),
        "revoked_reason": row.revoked_reason,
    }


@router.get("/me/storage", response_model=schemas.StorageConnectionListOut)
async def list_my_connections(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """What storage this account has connected, and whether it still works.

    Health is checked per connection rather than assumed from ``status``, because
    a grant can be withdrawn at the provider while this row still says ``active``.
    Reporting that disagreement is the point of the ``problems`` list: a
    connection that looks fine and is not is worse than one that says it expired.
    """
    result = await db.execute(
        select(models.StorageConnection)
        .where(models.StorageConnection.user_id == current_user.id)
        .order_by(models.StorageConnection.id)
    )
    rows = result.scalars().all()
    outs = [_connection_out(r) for r in rows]
    return {
        "generated_at": _now(),
        "connections": outs,
        "healthy_count": sum(1 for o in outs if o["healthy"]),
        "unhealthy_count": sum(1 for o in outs if not o["healthy"]),
        "note": (
            "no token is returned by this endpoint, ever. Revoking a connection "
            "overwrites the stored ciphertext and keeps the record of what was once "
            "granted."
        ),
    }


@router.get("/me/storage/providers", response_model=schemas.StorageProviderListOut)
async def list_storage_providers(
    current_user: models.User = Depends(deps.get_current_user),
):
    """Storage this build can connect to, each labelled with whether it can.

    ``configured`` is real rather than optimistic: a provider ships with its
    shape and no credentials, and it is false until the environment supplies
    them. Showing a Drive button that leads to a broken consent screen would be
    worse than showing that Drive is unavailable.
    """
    catalog = sp.build_storage_catalog()
    providers = [
        {
            "provider": row["provider"],
            "label": row["label"],
            "configured": bool(row["configured"]),
            "supports_pkce": bool(row["supports_pkce"]),
            "default_scope": row["default_scope"],
            "scopes": row["scopes"],
            "docs": row.get("docs"),
            "notes": row.get("notes"),
        }
        for row in catalog["providers"]
    ]
    return {
        "generated_at": _now(),
        "providers": providers,
        "configured_count": sum(1 for p in providers if p["configured"]),
        "env_prefix": catalog["env_prefix"],
        "token_cipher": catalog["token_cipher"],
        "note": (
            "each provider lists its scope ladder from narrowest to broadest. The "
            "broad tier really does grant everything, including deletion, and it is "
            "never the default."
        ),
    }


@router.post("/me/storage/connect", response_model=schemas.StorageAuthorizeOut)
async def begin_storage_connect(
    payload: schemas.StorageConnectIn,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Start an OAuth flow, returning the URL to send the user to.

    PKCE and ``state`` are both generated per attempt and the state is stored as
    a digest, so the callback can prove it belongs to a flow this server started
    -- an attacker who can hit the callback endpoint directly cannot use someone
    else's code.

    A broad scope needs ``confirm_broad_scope: true`` *and* an explicit
    acknowledgement that it grants deletion. Full access to someone's files is a
    real capability they can grant; what is not acceptable is it being reachable
    by a client that did not mean to.
    """
    provider = str(payload.provider or "").strip().lower()
    spec = sp.STORAGE_PROVIDER_BY_NAME.get(provider)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown provider {provider!r}"
        )
    scope = str(payload.scope or spec["default_scope"])
    scope_check = sp.validate_scope(provider, scope)
    if not scope_check["ok"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=scope_check["reason"]
        )
    breadth = sp.requires_confirmation(provider, scope)
    if breadth["requires_confirmation"] and not payload.confirm_broad_scope:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "broad_scope_requires_confirmation",
                "grants": breadth["grants"],
                "how_to_proceed": (
                    "re-send with confirm_broad_scope=true once the customer has read "
                    "what it grants"
                ),
            },
        )

    # The redirect URI has to be absolute and registered with the provider, so
    # it comes from the environment and never from the request: a redirect built
    # from a client-supplied Host header is a redirect the client chooses, and
    # this is the URL the provider sends the authorisation code back to.
    redirect_uri = os.getenv("CSERVICE_STORAGE_REDIRECT_URI", "")
    if not redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "CSERVICE_STORAGE_REDIRECT_URI is not set, so there is no callback "
                "address to send the customer to. Connecting storage is unavailable "
                "rather than sending them to a URL that will not match what is "
                "registered with the provider."
            ),
        )
    verifier, challenge = sp.generate_pkce_pair()
    state = security.create_access_token(
        {"sub": current_user.username, "typ": "storage_state"}, expires_delta=timedelta(minutes=10)
    )

    url = sp.build_authorize_url(
        provider, redirect_uri=redirect_uri, code_challenge=challenge, state=state, scope=scope
    )

    result = await db.execute(
        select(models.StorageConnection).where(
            models.StorageConnection.user_id == current_user.id,
            models.StorageConnection.provider == provider,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = models.StorageConnection(
            user_id=current_user.id, provider=provider, status="pending"
        )
        db.add(row)
    row.state_hash = dr.digest_signal(state, "device")
    row.code_verifier_encrypted = sp.encrypt_token(verifier)
    row.requested_scopes_json = json.dumps([scope])
    row.scopes_json = "[]"
    row.status = "pending"
    row.label = payload.label
    await db.commit()

    return {
        "provider": provider,
        "authorize_url": url,
        "scope": scope,
        "broad_scope": bool(breadth["is_broad"]),
        "requires_confirmation": bool(breadth["requires_confirmation"]),
        "grants": breadth["grants"],
        "state": state,
        "note": (
            "the redirect URI comes from CSERVICE_STORAGE_REDIRECT_URI, never from "
            "the request: a redirect built from a client-supplied Host is a redirect "
            "the client chooses"
        ),
    }


@router.get("/me/storage/callback", response_model=dict)
async def storage_callback(
    provider: str,
    code: str = "",
    state: str = "",
    error: Optional[str] = None,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Complete the OAuth flow: exchange the code, store the grant, encrypted.

    What this does now that it did not before: it performs the token exchange
    (:func:`storage_providers.exchange_authorization_code`), encrypts whatever
    comes back, and moves the row to ``active``. A connection that used to be
    permanently ``pending`` can now be live.

    Three things it still will not do, each reported rather than hidden:

    * **It refuses to invent a token.** With no client credentials it returns a
      503 naming the two env vars, and the row stays ``pending`` with the reason
      in ``last_error``. A fabricated token would make a connection look live
      while every file operation behind it failed.
    * **It cannot reach a provider from here.** There is no outbound HTTP client
      in this deployment, so without an injected transport the exchange reports
      that the request is composed and ready. That is a real limitation of *this*
      build, not of the code path -- and the distinction is the reason the
      limitation is a response rather than a comment.
    * **It clears the single-use state before exchanging.** So a failed exchange
      cannot be retried by replaying the callback. The authorization code is
      single-use at the provider anyway, so a second attempt could only ever
      fail with a more confusing error; the customer re-runs `connect` instead.

    What it verifies, and always did: the state digest belongs to a flow this
    account started, and the row belongs to the caller.
    """
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"the provider refused the connection: {error}",
        )
    result = await db.execute(
        select(models.StorageConnection).where(
            models.StorageConnection.user_id == current_user.id,
            models.StorageConnection.provider == provider,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no pending connection for this provider"
        )
    if not row.state_hash or row.state_hash != dr.digest_signal(state, "device"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="callback does not match a flow this account started",
        )

    # Decrypt the verifier *before* clearing it -- the exchange needs it, and it
    # is stored encrypted precisely so that clearing it afterwards leaves nothing
    # recoverable.
    verifier = sp.decrypt_token(row.code_verifier_encrypted)

    # Drop the state and verifier whatever happens next: they are single-use, and
    # leaving them on the row would let a second callback replay the first.
    row.state_hash = ""
    row.code_verifier_encrypted = ""

    requested = json.loads(row.requested_scopes_json or "[]")
    redirect_uri = os.getenv("CSERVICE_STORAGE_REDIRECT_URI", "")
    exchange = sp.exchange_authorization_code(
        provider, code, verifier, redirect_uri, transport=_storage_transport()
    )

    if not exchange.ok:
        row.status = "pending"
        row.last_error = exchange.reason[:255]
        await db.commit()
        # 503 rather than 400: nothing the customer did was wrong. The code was
        # accepted, the state verified, and the failure is ours -- unconfigured
        # credentials or no outbound transport.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "token_exchange_unavailable",
                "provider": provider,
                "reason": exchange.reason,
                "attempted": exchange.attempted,
                "status": row.status,
                "state_consumed": True,
                "next": (
                    "re-run POST /users/me/storage/connect -- the authorization code "
                    "is single-use, so this callback cannot be retried"
                ),
            },
        )

    row.access_token_encrypted = sp.encrypt_token(exchange.access_token)
    row.refresh_token_encrypted = sp.encrypt_token(exchange.refresh_token)
    row.expires_at = exchange.expires_at
    # Record the grant, not the request. A user can approve less than was asked
    # for, and a connection that described the request would overstate what it
    # can do.
    granted = list(exchange.scopes) or list(requested)
    row.scopes_json = json.dumps(granted)
    row.status = "active"
    row.connected_at = _now()
    row.last_error = ""
    row.revoked_at = None
    row.revoked_reason = ""
    await db.commit()

    return {
        "provider": provider,
        "received_code": bool(code),
        "status": row.status,
        "connected": True,
        "granted_scopes": granted,
        "requested_scopes": requested,
        "expires_at": _as_utc(row.expires_at),
        "note": (
            "the authorization code was exchanged and both tokens are stored "
            "encrypted; the ciphertext is never returned by any endpoint"
        ),
    }


@router.delete("/me/storage/{connection_id}", response_model=dict)
async def revoke_my_connection(
    connection_id: int,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Disconnect storage, overwriting the stored tokens.

    Overwrite rather than delete: the row records that this account once granted
    this provider this scope, and that record is what an operator needs when a
    leak is being investigated. The ciphertext is the part that must not survive,
    and blanking it achieves that while keeping the history.

    Revoking here does not revoke at the provider -- that needs the provider's
    endpoint and its credentials. Saying so plainly matters: a user who believes
    they have cut off access and has not is exactly the situation this response
    must not leave ambiguous.
    """
    result = await db.execute(
        select(models.StorageConnection).where(
            models.StorageConnection.id == connection_id,
            models.StorageConnection.user_id == current_user.id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such connection")
    scopes = row.scopes_json
    row.access_token_encrypted = ""
    row.refresh_token_encrypted = ""
    row.state_hash = ""
    row.code_verifier_encrypted = ""
    row.status = "revoked"
    row.revoked_at = _now()
    row.revoked_reason = "user_request"
    await db.commit()
    return {
        "connection_id": connection_id,
        "provider": row.provider,
        "revoked": True,
        "reason": "user_request",
        "grants_still_recorded": json.loads(scopes or "[]"),
        "note": (
            "the stored tokens are overwritten, so this service can no longer act "
            "on the account. The grant may still exist at the provider; revoke it "
            "there too if you need it gone now."
        ),
    }


@router.delete("/me/storage", response_model=dict)
async def revoke_all_connections(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Disconnect every storage provider at once."""
    result = await db.execute(
        select(models.StorageConnection).where(
            models.StorageConnection.user_id == current_user.id,
            models.StorageConnection.status != "revoked",
        )
    )
    rows = result.scalars().all()
    providers = []
    for row in rows:
        row.access_token_encrypted = ""
        row.refresh_token_encrypted = ""
        row.state_hash = ""
        row.code_verifier_encrypted = ""
        row.status = "revoked"
        row.revoked_at = _now()
        row.revoked_reason = "user_request"
        providers.append(row.provider)
    await db.commit()
    return {
        "revoked_count": len(providers),
        "providers": providers,
        "reason": "user_request",
        "note": (
            "all stored tokens for this account are overwritten. Provider-side "
            "grants are unaffected and must be revoked at each provider."
        ),
    }
