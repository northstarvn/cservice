"""Outbound mail: delivering the one-time codes that make sign-in work.

Why this module exists
----------------------
``POST /users/auth/email-otp/request`` generates a code, hashes it, and stores
the hash. That is a complete *credential* and an incomplete *feature*: without
something to deliver the plaintext, the only way to sign in with a one-time code
is to read the database, which makes the whole bootstrap path untestable and the
endpoint useless to a real customer. This is the delivery half.

The transport is a boundary, not an implementation
--------------------------------------------------
:func:`deliver` dispatches to exactly one of three transports, chosen by
``CSERVICE_MAIL_TRANSPORT``:

``smtp``
    A real SMTP conversation. Credentials and host from the environment.

``file``
    Appends each message to a directory as JSON. Useful for a staging box and for
    inspecting exactly what a customer would receive.

``simulated``
    Records the message in memory and returns success **without sending
    anything**. This is the default, and that is a decision worth being explicit
    about: a deployment that has not configured mail should not silently
    generate codes it cannot deliver, so the endpoint reports what actually
    happened rather than claiming a code is on its way.

The default is ``simulated`` rather than a hard failure because a developer
running this locally needs the OTP path to work end to end, and a deployment
that has *not* configured SMTP needs to be told so rather than have a
connection attempt hang. What it must never be is silently believed: see
:func:`transport_reports_delivery`.

The honesty rule
----------------
Every call returns a :class:`DeliveryResult` carrying ``delivered`` -- meaning
the code reached a channel that can put it in front of a person -- separately
from ``accepted``. A simulated send is ``accepted`` and not ``delivered``, and
the endpoint response says which. Without that split, the failure mode is a
sign-in flow that reports "check your email" for every request forever, which
reads as a broken mail server rather than an unconfigured one.

What is not here
----------------
No templating, no attachments, no queue. Sending synchronously on the request
path is a deliberate simplification: the alternative is a background worker whose
failure is invisible at the call site, and a code that is silently never
delivered is worse than a slow request. :func:`deliver`'s docstring says where
that trade should be revisited.
"""

from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

#: Version of the shipped transport table.
MAIL_TRANSPORT_VERSION = "mail_transport_v1"

#: `CSERVICE_MAIL_TRANSPORT`. An unrecognised value falls back to `simulated`
#: for the same reason recognition's mode does: the safest reading of a typo is
#: the one that cannot claim something happened that did not.
MAIL_TRANSPORT_ENV = "CSERVICE_MAIL_TRANSPORT"
MAIL_TRANSPORT_DEFAULT = "simulated"

MAIL_TRANSPORTS: tuple[str, ...] = ("simulated", "file", "smtp")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DeliveryResult:
    """What happened, distinguishing *accepted* from *delivered*.

    ``delivered`` is the field that matters. Accepted means the message was
    handed to a transport without error; delivered means it can actually reach
    the recipient. Only the second one justifies telling a customer to check
    their inbox.
    """

    transport: str
    accepted: bool
    delivered: bool
    detail: str = ""
    recipient_masked: str = ""
    sent_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "accepted": self.accepted,
            "delivered": self.delivered,
            "detail": self.detail,
            "recipient_masked": self.recipient_masked,
            "sent_at": self.sent_at,
        }


class SimulatedOutbox:
    """In-memory record of messages a simulated transport accepted.

    A class attribute rather than a module global so a test can clear it, and
    bounded so a long-running process in the default configuration does not
    accumulate every code it ever generated in memory.

    It holds **plaintext codes**, which is the whole point and also the risk. It
    is in-memory, bounded, never logged, and only reachable in the `simulated`
    transport -- which is not the transport a production deployment should be
    running. :func:`transport_reports_delivery` is what stops that from being
    mistaken for a working mail configuration.
    """

    #: Bounded so the default transport cannot become a memory leak in a
    #: long-running process. 256 is comfortably more than a test needs and
    #: small enough to be irrelevant.
    limit: int = 256
    _messages: list[dict[str, Any]] = []

    @classmethod
    def record(cls, entry: dict[str, Any]) -> None:
        cls._messages.append(entry)
        if len(cls._messages) > cls.limit:
            del cls._messages[: len(cls._messages) - cls.limit]

    @classmethod
    def all(cls) -> list[dict[str, Any]]:
        return list(cls._messages)

    @classmethod
    def last(cls) -> Optional[dict[str, Any]]:
        return cls._messages[-1] if cls._messages else None

    @classmethod
    def clear(cls) -> None:
        cls._messages.clear()


def mask(recipient: str) -> str:
    """Masked form of an address, for logs and results.

    A delivery log that carries the full address is a log of who was sent a
    sign-in credential, which is a different and worse artifact than a log of
    how many were sent.
    """
    text = str(recipient or "")
    if "@" in text:
        local, _, domain = text.partition("@")
        head = local[:1] if local else ""
        return f"{head}***@{domain}"
    return "***" if text else ""


def configured_transport() -> str:
    """The transport in force, falling back on anything unrecognised."""
    raw = str(os.getenv(MAIL_TRANSPORT_ENV, "") or "").strip().lower()
    return raw if raw in MAIL_TRANSPORTS else MAIL_TRANSPORT_DEFAULT


def transport_reports_delivery(transport: Optional[str] = None) -> bool:
    """Whether this transport can put a message in front of a person.

    Only `smtp` and `file` can. `simulated` cannot, and this is the single
    place that fact is recorded, so the endpoint does not have to re-derive it
    from a transport name each time.
    """
    return (transport or configured_transport()) in {"smtp", "file"}


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------


def build_otp_message(
    *,
    to: str,
    code: str,
    purpose: str = "login",
    ttl_minutes: int = 10,
    app_name: str = "CService",
) -> dict[str, Any]:
    """The message for a one-time code.

    Carries the code, the purpose and the expiry, because a code the recipient
    cannot tell apart from another is a code they will enter into the wrong
    form. Deliberately plain text: a branded template is a design decision, and
    this is a credential.

    The plaintext code is returned rather than sent straight into a transport so
    a caller can audit what was composed -- which is the only reason the
    simulated transport's outbox is trustworthy.
    """
    body = (
        f"Your {app_name} sign-in code is {code}.\n\n"
        f"It is for {purpose} and expires in {ttl_minutes} minutes.\n"
        f"If you did not request it, you can ignore this message -- nobody can "
        f"sign in without it.\n"
    )
    return {
        "to": to,
        "subject": f"Your {app_name} sign-in code",
        "body": body,
        "purpose": purpose,
        "expires_in_minutes": ttl_minutes,
    }


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


def _deliver_simulated(message: dict[str, Any]) -> DeliveryResult:
    """Record the message and report that nothing was actually sent.

    Returns ``delivered=False`` deliberately. The message is stored so the flow
    is testable end to end, but a caller that reads ``delivered`` learns the
    truth, and the endpoint's response is built from that rather than from the
    assumption that generation implies delivery.
    """
    SimulatedOutbox.record(
        {
            "to": message.get("to", ""),
            "subject": message.get("subject", ""),
            "body": message.get("body", ""),
            "purpose": message.get("purpose", ""),
            "recorded_at": _now().isoformat(),
        }
    )
    return DeliveryResult(
        transport="simulated",
        accepted=True,
        delivered=False,
        detail=(
            "recorded in the in-memory outbox; no mail was sent. Set "
            f"{MAIL_TRANSPORT_ENV}=smtp or file to deliver"
        ),
        recipient_masked=mask(str(message.get("to", ""))),
    )


def _deliver_file(message: dict[str, Any]) -> DeliveryResult:
    """Append the message to ``CSERVICE_MAIL_DIR`` as one JSON file.

    Deliberately one file per message rather than one JSON array: concurrent
    writers to a single file interleave, and this is a directory an operator may
    tail, grep, or serve.
    """
    directory = Path(os.getenv("CSERVICE_MAIL_DIR", "./var/mail")).expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y%m%dT%H%M%S%f")
        target = directory / f"{stamp}-{abs(hash(str(message.get('to', '')))) % 10**8}.json"
        target.write_text(json.dumps({**message, "written_at": _now().isoformat()}, indent=2))
    except OSError as exc:
        # Accepted is False here: the transport was chosen and reachable but the
        # write failed, which is a real delivery failure rather than a
        # simulated one. Reporting it as "not configured" would send an operator
        # looking in the wrong place.
        return DeliveryResult(
            transport="file",
            accepted=False,
            delivered=False,
            detail=f"could not write to {directory}: {exc}",
            recipient_masked=mask(str(message.get("to", ""))),
        )
    return DeliveryResult(
        transport="file",
        accepted=True,
        delivered=True,
        detail=f"written to {target}",
        recipient_masked=mask(str(message.get("to", ""))),
    )


def _deliver_smtp(message: dict[str, Any]) -> DeliveryResult:
    """A real SMTP send.

    Credentials come from the environment and are never echoed. A send that
    raises is reported as a failure with the exception's class, not its message,
    because an SMTP exception can quote the credentials it was given.
    """
    host = os.getenv("CSERVICE_SMTP_HOST", "")
    port = int(os.getenv("CSERVICE_SMTP_PORT", "587") or 587)
    username = os.getenv("CSERVICE_SMTP_USER", "")
    password = os.getenv("CSERVICE_SMTP_PASSWORD", "")
    sender = os.getenv("CSERVICE_SMTP_FROM", "") or username
    use_tls = str(os.getenv("CSERVICE_SMTP_TLS", "1")).strip() not in {"0", "false", "no"}

    if not host:
        return DeliveryResult(
            transport="smtp",
            accepted=False,
            delivered=False,
            detail=f"{MAIL_TRANSPORT_ENV}=smtp but CSERVICE_SMTP_HOST is unset",
            recipient_masked=mask(str(message.get("to", ""))),
        )

    mail = EmailMessage()
    mail["Subject"] = str(message.get("subject", ""))
    mail["From"] = sender or "no-reply@localhost"
    mail["To"] = str(message.get("to", ""))
    mail.set_content(str(message.get("body", "")))

    try:
        with smtplib.SMTP(host, port, timeout=15) as client:
            if use_tls:
                client.starttls()
            if username and password:
                client.login(username, password)
            client.send_message(mail)
    except Exception as exc:  # noqa: BLE001 -- any SMTP failure is a delivery failure
        # The class name only. An smtplib exception can quote the credentials
        # it was handed, and this string can reach a log.
        return DeliveryResult(
            transport="smtp",
            accepted=False,
            delivered=False,
            detail=f"{type(exc).__name__} while sending over SMTP to {host}",
            recipient_masked=mask(str(message.get("to", ""))),
        )

    return DeliveryResult(
        transport="smtp",
        accepted=True,
        delivered=True,
        detail=f"sent via {host}",
        recipient_masked=mask(str(message.get("to", ""))),
    )


_TRANSPORTS = {
    "simulated": _deliver_simulated,
    "file": _deliver_file,
    "smtp": _deliver_smtp,
}


def deliver(message: dict[str, Any], *, transport: Optional[str] = None) -> DeliveryResult:
    """Send one message, reporting honestly whether it was delivered.

    Never raises: a delivery failure is a result, not an exception, because the
    caller's job is to record that the credential was generated and tell the
    user so -- and an exception here would leave the stored hash behind with no
    way to explain it.
    """
    chosen = str(transport or configured_transport())
    handler = _TRANSPORTS.get(chosen)
    if handler is None:
        return DeliveryResult(
            transport=chosen,
            accepted=False,
            delivered=False,
            detail=f"unknown transport {chosen!r}; falling back to the default",
            recipient_masked=mask(str(message.get("to", ""))),
        )
    return handler(message)


# ---------------------------------------------------------------------------
# Validation + catalog
# ---------------------------------------------------------------------------

MAIL_CODES: dict[str, str] = {
    "unknown_transport": "a transport name that does not exist",
    "default_cannot_deliver": "the default transport cannot deliver, so codes are generated and discarded",
    "smtp_unconfigured": "smtp is selected but the host is unset",
    "outbox_unbounded": "the simulated outbox is unbounded",
    "plaintext_in_result": "a delivery result carries the plaintext code",
    "credential_in_detail": "a failure detail can quote credentials",
}

#: Keys a :class:`DeliveryResult` may not contain. A code in a result is a code
#: in whatever the result is written into -- a response body, a log line, a
#: metrics label -- and the request path has no way to unwrite it.
FORBIDDEN_RESULT_KEYS: tuple[str, ...] = ("code", "body", "password", "token")


def validate_mail() -> dict[str, Any]:
    """Check the shipped transport configuration.

    ``default_cannot_deliver`` is a *warning* about the environment, not an
    error about the code: a developer running locally wants the OTP path to work
    without a mail server, and a deployment that has not configured one needs to
    be told rather than left guessing.
    """
    findings: list[dict[str, Any]] = []
    active = configured_transport()

    if active not in MAIL_TRANSPORTS:
        findings.append(
            {
                "severity": "error",
                "code": "unknown_transport",
                "detail": f"{MAIL_TRANSPORT_ENV}={active!r} is not one of {', '.join(MAIL_TRANSPORTS)}",
            }
        )

    if not transport_reports_delivery(active):
        findings.append(
            {
                "severity": "warning",
                "code": "default_cannot_deliver",
                "detail": (
                    f"{active!r} cannot deliver: codes are generated, hashed and stored, "
                    "and recorded in memory, but no message reaches anyone. The "
                    "endpoint reports this rather than claiming a code is on its way."
                ),
            }
        )

    if active == "smtp" and not os.getenv("CSERVICE_SMTP_HOST", ""):
        findings.append(
            {
                "severity": "error",
                "code": "smtp_unconfigured",
                "detail": "transport is smtp but CSERVICE_SMTP_HOST is unset, so every send fails",
            }
        )

    if SimulatedOutbox.limit <= 0:
        findings.append(
            {
                "severity": "error",
                "code": "outbox_unbounded",
                "detail": "the simulated outbox must be bounded or it is a memory leak",
            }
        )

    # The outbox holding plaintext is intended. It holding an unbounded amount
    # of it, or a result echoing it, are not.
    result_fields = set(DeliveryResult.__dataclass_fields__)
    leaked = result_fields & set(FORBIDDEN_RESULT_KEYS)
    if leaked:
        findings.append(
            {
                "severity": "error",
                "code": "plaintext_in_result",
                "detail": (
                    f"DeliveryResult carries {sorted(leaked)}; a code in a result is a "
                    "code in whatever the result is serialised into"
                ),
            }
        )

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return {
        "generated_at": _now(),
        "version": MAIL_TRANSPORT_VERSION,
        "transports": list(MAIL_TRANSPORTS),
        "active": active,
        "reports_delivery": transport_reports_delivery(active),
        "env": MAIL_TRANSPORT_ENV,
        "codes": dict(MAIL_CODES),
        "findings": findings,
        "counts_by_severity": counts,
        "ok": counts.get("error", 0) == 0,
        "simulated_outbox": {
            "messages": len(SimulatedOutbox.all()),
            "limit": SimulatedOutbox.limit,
            "holds_plaintext_codes": True,
        },
        "note": (
            "accepted means the transport took the message; delivered means it can "
            "reach the recipient. Only the second justifies telling someone to check "
            "their inbox, and the simulated transport never reports it."
        ),
    }


def build_mail_catalog() -> dict[str, Any]:
    """Introspection payload for ``/meta/scoring-catalog``."""
    return {
        "version": MAIL_TRANSPORT_VERSION,
        "env": MAIL_TRANSPORT_ENV,
        "transports": list(MAIL_TRANSPORTS),
        "active": configured_transport(),
        "reports_delivery": transport_reports_delivery(),
        "smtp_env": {
            name: bool(os.getenv(f"CSERVICE_SMTP_{name}"))
            for name in ("HOST", "PORT", "USER", "PASSWORD", "FROM", "TLS")
        },
        "file_dir_env": "CSERVICE_MAIL_DIR",
        "note": (
            "the simulated transport is the default and cannot deliver. It exists so "
            "the one-time-code path works end to end without a mail server; it "
            "records plaintext codes in a bounded in-memory outbox and the endpoint "
            "says so in its response rather than claiming the code is on its way."
        ),
    }
