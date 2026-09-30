"""The one-way shadow environment: a second deployment that runs changed code
against its own data, and cannot reach live.

Why a second process, and not an in-process twin
-----------------------------------------------
"Can I run the changed code without touching live" has an easy answer and a hard
one. The easy answer is an in-process twin: a second engine, a second session,
candidate logic injected as a pluggable callable. The hard answer is that Python
cannot load two versions of a module into one interpreter without importlib
tricks that share ``sys.modules``, share every global the candidate closed over,
and share the process's memory. At that point "the changed code" is running in
the same address space as live code, and the isolation claim is a comment.

So the shadow is a real deployment: its own checkout, its own database, its own
process, started by ``scripts/run_shadow_env.py``. This module is not the runner.
It is the part that makes the deployment *checkable* -- the identity comparison
that proves the shadow is pointed somewhere else, the channel table that makes
every replicated feed declare its direction, and the validators that turn
"we were careful" into "and here is the evidence".

The one-way rule
----------------
Everything here reduces to one sentence: **data flows live → shadow, and nothing
flows shadow → live.** That is not a convention, it is a direction carried on
every feed and re-derived on every validation, so a channel that claims to be
one-way while pointing at the live database is an *error*, not a note.

**Keep-as-is decision:** the guard compares database *identity*
``(host, port, database)``, not host. A shadow database on the same postgres
instance is a perfectly good shadow -- the expensive part of this system is the
schema, not the server -- and a guard that demanded a different host would be
refused by every developer who runs both locally and would then be bypassed by
everyone. Identity is the thing that actually decides whether a write lands in
live, so identity is what is compared.

**Keep-as-is decision:** `evaluate_isolation` never raises and never refuses to
answer. A guard that throws tells you it failed and nothing about *which* check
failed, and it fails on the one input you most need a report for -- a malformed
URL. Every guard here returns a verdict with a per-check breakdown, and the
caller decides whether a failing verdict is fatal.

**Keep-as-is decision:** an unknown or missing measurement **fails closed**. A
guard that treats "we did not check" as "fine" is worse than no guard, because
it converts an unmeasured risk into a published safety claim. :func:`observed`
below is the only way to feed a measurement to a check, and it records absence
as absence rather than as a pass.

The seven-part skeleton, same as the rest of the backend
-------------------------------------------------------
1. ``SHADOW_ENVIRONMENTS`` / ``DATA_DIRECTIONS`` / ``ISOLATION_CHECKS`` /
   ``SHADOW_FEEDS``, each with a ``_BY_ID`` index and a pinned
   ``SHADOW_CATALOG_VERSION``.
2. A pure evaluator that returns a verdict and **never raises** --
   :func:`evaluate_isolation`.
3. Gate severities folded to one word (``advisory|blocking`` -> ``pass|fail``),
   failing closed on an unknown check *and* on a missing measurement.
4. A structured verdict shaped like ``risk_evaluator.escalation_for``, so
   ``isolated: False`` is a reasoned outcome rather than silence.
5. :func:`validate_shadow_env` separating *errors* (the deployment is unsafe)
   from *warnings* (something is configured but not doing anything).
6. :func:`build_shadow_env_catalog` returning the tables, their operators, the
   live verdict, and the derived feed directions.
7. A router surface on ``/kaizen/admin/shadow`` -- see ``app/routers/kaizen.py``.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import unquote, urlsplit

SHADOW_CATALOG_VERSION = 1

#: Bumped when a row below changes shape. Pinned so a verdict can name the
#: contract it was produced under instead of implying it is timeless.
SHADOW_ENVIRONMENTS: tuple[dict[str, Any], ...] = (
    {
        "environment": "live",
        "role": "production",
        "serves_traffic": True,
        "writable": True,
        "writable_targets": ("live_database",),
        "description": (
            "The deployment customers are served from. Unreachable from a shadow "
            "process: this is the whole point of the table."
        ),
    },
    {
        "environment": "shadow",
        "role": "pre-production",
        "serves_traffic": False,
        "writable": True,
        "writable_targets": ("shadow_database",),
        "description": (
            "Runs changed code against its own data so the change can be observed "
            "before it is deployed. Writable -- it is a real deployment, not a read "
            "replica -- but only ever into its own database."
        ),
    },
)
SHADOW_ENVIRONMENT_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["environment"]): dict(row) for row in SHADOW_ENVIRONMENTS
}
SHADOW_ENVIRONMENT_NAMES: tuple[str, ...] = tuple(
    str(row["environment"]) for row in SHADOW_ENVIRONMENTS
)
LIVE_ENVIRONMENT = "live"
SHADOW_ENVIRONMENT = "shadow"

#: The direction every data channel must move in. Declared as data so a channel
#: that runs the wrong way is caught by arithmetic over this table rather than
#: by reading the code and hoping.
DATA_DIRECTIONS: tuple[dict[str, Any], ...] = (
    {
        "direction": "live_to_live",
        "source": "live",
        "target": "live",
        "permitted": True,
        "description": "ordinary production traffic inside the live deployment",
    },
    {
        "direction": "live_to_shadow",
        "source": "live",
        "target": "shadow",
        "permitted": True,
        "description": (
            "the only channel a shadow needs. Read-only against live, written "
            "into the shadow's own database."
        ),
    },
    {
        "direction": "shadow_to_shadow",
        "source": "shadow",
        "target": "shadow",
        "permitted": True,
        "description": "the shadow exercising itself; the normal case while simulating",
    },
    {
        "direction": "shadow_to_live",
        "source": "shadow",
        "target": "live",
        "permitted": False,
        "description": (
            "FORBIDDEN. This is the direction that turns a simulation into an "
            "incident, and it is the single rule the whole subsystem exists to make "
            "structurally impossible rather than merely discouraged."
        ),
    },
)
DATA_DIRECTION_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["direction"]): dict(row) for row in DATA_DIRECTIONS
}
FORBIDDEN_DIRECTION = "shadow_to_live"
FORBIDDEN_DIRECTION_IDS: tuple[str, ...] = tuple(
    str(row["direction"]) for row in DATA_DIRECTIONS if not row["permitted"]
)

#: What the isolation verdict actually proves. Each check is a separate row so a
#: failure names itself: "the shadow is pointed at the live database" and "the
#: shadow process is not marked as a shadow" are different problems with
#: different fixes, and collapsing them into one boolean loses that.
ISOLATION_CHECKS: tuple[dict[str, Any], ...] = (
    {
        "check_id": "database_identity",
        "severity": "blocking",
        "asserts": "the shadow's database identity differs from live's",
        "inputs": ("live_url", "shadow_url"),
        "remedy": (
            "give the shadow its own database (CSERVICE_SHADOW_DATABASE_URL) and run "
            "its migrations; do not point the shadow process at DATABASE_URL"
        ),
    },
    {
        "check_id": "environment_marker",
        "severity": "blocking",
        "asserts": "the shadow process is marked shadow and not live",
        "inputs": ("shadow_env_name",),
        "remedy": "set CSERVICE_ENV=shadow in the shadow process environment",
    },
    {
        "check_id": "write_scope",
        "severity": "blocking",
        "asserts": "every writable target the shadow declares is non-live",
        "inputs": ("shadow_write_targets",),
        "remedy": "remove the live target; a shadow that can write live is not a shadow",
    },
    {
        "check_id": "feed_direction",
        "severity": "blocking",
        "asserts": "every replicated channel runs in a permitted direction",
        "inputs": ("feeds",),
        "remedy": (
            "point the channel at the shadow database; a shadow_to_live channel is a "
            "data pipeline writing to customers"
        ),
    },
    {
        "check_id": "live_read_only",
        "severity": "advisory",
        "asserts": "the live database is reached for replication by a read-only principal",
        "inputs": ("live_read_only",),
        "remedy": (
            "replication should use a SELECT-only role so that a bug in the shadow's "
            "replicator cannot become a write even if the direction table is wrong"
        ),
    },
    {
        "check_id": "egress_allowlist",
        "severity": "advisory",
        "asserts": "the shadow may not call an unrestricted third party",
        "inputs": ("shadow_egress_allowlist",),
        "remedy": (
            "an unrestricted egress from a shadow lets a code change reach the outside "
            "world from an environment nobody is watching"
        ),
    },
)
ISOLATION_CHECK_BY_ID: dict[str, dict[str, Any]] = {
    str(row["check_id"]): dict(row) for row in ISOLATION_CHECKS
}
ISOLATION_CHECK_IDS: tuple[str, ...] = tuple(
    str(row["check_id"]) for row in ISOLATION_CHECKS
)

#: The channels that carry live data into the shadow. Every row's ``direction``
#: is *re-derived* from its ``source``/``target`` during validation and compared
#: against the declared value, so a row cannot lie by declaring a safe direction
#: while pointing somewhere else.
SHADOW_FEEDS: tuple[dict[str, Any], ...] = (
    {
        "feed_id": "identity",
        "source": "live",
        "target": "shadow",
        "direction": "live_to_shadow",
        "mode": "stream",
        "tables": ("users", "user_preference_profiles", "user_consent_events"),
        "scrub": ("hashed_password", "api_key_hash"),
        "description": "accounts and the preferences/consent that decide what they may be told",
    },
    {
        "feed_id": "booking_state",
        "source": "live",
        "target": "shadow",
        "direction": "live_to_shadow",
        "mode": "stream",
        "tables": ("bookings", "booking_events", "booking_assignments"),
        "scrub": (),
        "description": "the booking lifecycle, including the event log a rollback has to reason about",
    },
    {
        "feed_id": "conversation",
        "source": "live",
        "target": "shadow",
        "direction": "live_to_shadow",
        "mode": "stream",
        "tables": ("chat_history", "interaction_signals"),
        "scrub": ("message",),
        "description": (
            "transcripts drive sentiment and retention, so a shadow without them "
            "simulates a system nobody has. The message body is scrubbed and the "
            "shape is kept, because the engines read shape."
        ),
    },
    {
        "feed_id": "policy_state",
        "source": "live",
        "target": "shadow",
        "direction": "live_to_shadow",
        "mode": "stream",
        "tables": ("customer_policy_scores", "user_communication_overrides"),
        "scrub": (),
        "description": "tier, band and override; the inputs every risk decision reads",
    },
    {
        "feed_id": "retention_state",
        "source": "live",
        "target": "shadow",
        "direction": "live_to_shadow",
        "mode": "snapshot",
        "tables": ("retention_snapshots", "recovery_actions", "recovery_outcomes"),
        "scrub": (),
        "description": "the series the health banding and forecasting read; snapshot, not stream",
    },
    {
        "feed_id": "money_state",
        "source": "live",
        "target": "shadow",
        "direction": "live_to_shadow",
        "mode": "stream",
        "tables": ("points_wallets", "points_transactions", "arrears_entries"),
        "scrub": (),
        "description": (
            "wallets, transactions and arrears. Replicated because recovery playbooks "
            "issue goodwill from these balances; not replicating them makes a "
            "simulation that spends money nobody has."
        ),
    },
    {
        "feed_id": "complaint_state",
        "source": "live",
        "target": "shadow",
        "direction": "live_to_shadow",
        "mode": "stream",
        "tables": ("complaint_cases", "complaint_events", "complaint_decisions"),
        "scrub": ("resolution_note",),
        "description": "cases in flight, so escalation and SLA behaviour can be observed",
    },
)
SHADOW_FEED_BY_ID: dict[str, dict[str, Any]] = {
    str(row["feed_id"]): dict(row) for row in SHADOW_FEEDS
}
SHADOW_FEED_IDS: tuple[str, ...] = tuple(str(row["feed_id"]) for row in SHADOW_FEEDS)

#: Fields that must never cross into a shadow even with replication on, because
#: a simulation has no need of them and a leak is not undoable.
NEVER_REPLICATED_FIELDS: tuple[str, ...] = ("password", "token", "secret", "api_key", "ssn")

_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

#: A DNS hostname. Used, together with :mod:`ipaddress`, to tell a real host from
#: a sentence: ``postgresql://not a url at all`` parses without error and yields
#: a "hostname" of four words, which must not read as comparable.
_HOSTNAME_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$"
)


def _plausible_host(host: str) -> bool:
    """Is this a host we can believe, by name or by address?

    ``urlsplit`` strips the brackets from an IPv6 literal, so ``[::1]`` arrives
    here as ``::1`` and has to be checked as an address rather than as a DNS
    name -- which is also why this is a function and not one regex.
    """
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME_RE.match(host))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _str_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a config value to a tuple of non-empty strings, never raising.

    A guard is handed whatever the operator typed into an environment variable,
    and ``None.split(",")`` would turn a typo into a stack trace in the one place
    whose job is to explain what is wrong.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(part).strip() for part in value if str(part).strip())
    return (str(value).strip(),) if str(value).strip() else ()


# ---------------------------------------------------------------------------
# Database identity


def database_identity(url: Any) -> dict[str, Any]:
    """The ``(host, port, database)`` triple that decides where a write lands.

    Returns a structured verdict with ``comparable: False`` rather than raising
    on an unparseable URL, because an operator pasting a broken value into the
    shadow's environment is exactly the case this function exists to diagnose.

    **Comparable means both halves were found.** A URL with no database segment
    (``postgresql://host``) is *not* comparable, and neither is a host that looks
    like a sentence rather than a hostname. An earlier version treated both as
    comparable-with-an-empty-database on the reasoning that "an unnamed target is
    not the live one", which is the fail-*open* direction: pasting
    ``not a url at all`` produced a distinct identity, the shadow compared
    unequal to live, and the guard reported ``isolated: true`` -- green because
    the operator made a typo. What "comparable" has to mean is *we can tell*,
    and for an identity with no database we cannot.
    """
    text = str(url or "").strip()
    if not text:
        return {
            "url": "",
            "comparable": False,
            "scheme": "",
            "host": "",
            "port": None,
            "database": "",
            "user": "",
            "note": "no url supplied; identity cannot be compared",
        }
    if not _URL_SCHEME_RE.match(text):
        # Accept the alembic.ini spelling, which omits the driver.
        text = "postgresql://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return {
            "url": text,
            "comparable": False,
            "scheme": "",
            "host": "",
            "port": None,
            "database": "",
            "user": "",
            "note": "url could not be parsed; identity cannot be compared",
        }
    database = unquote(parts.path or "").lstrip("/")
    host = parts.hostname or ""
    plausible = _plausible_host(host)
    comparable = bool(plausible and database)
    note = ""
    if not comparable:
        note = (
            "url names no database; identity cannot be compared"
            if plausible
            else "url names no host; identity cannot be compared"
        )
    return {
        "url": text,
        "comparable": comparable,
        "scheme": parts.scheme,
        "host": host,
        "port": parts.port,
        "database": database,
        "user": unquote(parts.username or ""),
        "note": note,
    }


def identity_key(identity: Mapping[str, Any]) -> str:
    """A single comparable string for two identities.

    **The whole credential is excluded -- user and password both.** Two urls
    differing only in credentials name the same database, and including any part
    of the credential would report "isolated" for a shadow that is in fact
    writing live with a different login. That is the specific mistake this
    function is easy to get wrong: an earlier version excluded the password and
    kept the user, so ``alice@/cservice`` and ``bob@/cservice`` compared as
    different databases -- a guard that passes precisely when a developer adds a
    read-only replication role, which is the configuration most likely to be
    followed by a write.

    What is left is ``(scheme, host, port, database)``: the thing a rollback or a
    dump would actually move.
    """
    if not identity.get("comparable"):
        return ""
    return "{scheme}://{host}:{port}/{database}".format(
        scheme=str(identity.get("scheme") or ""),
        host=str(identity.get("host") or ""),
        port=str(identity.get("port") or ""),
        database=str(identity.get("database") or ""),
    )


def same_database(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True when two identities name the same database.

    Fails closed: an identity that could not be compared counts as *the same*
    database, because the whole point of this predicate is to catch the shadow
    that writes live, and "we could not tell" is not a licence to proceed.
    """
    left_key = identity_key(left)
    right_key = identity_key(right)
    if not left_key or not right_key:
        return True
    return left_key == right_key


# ---------------------------------------------------------------------------
# Direction arithmetic


def derive_direction(source: Any, target: Any) -> str:
    """The channel direction implied by a source and a target environment.

    Exists so ``SHADOW_FEEDS``' declared ``direction`` can be *checked* rather
    than trusted. The table is the kind of thing that gets edited by hand, and a
    row edited to say ``live_to_shadow`` while pointing at ``live`` is precisely
    the bug that would not otherwise be noticed until a customer's data moved.
    """
    src = str(source or "").strip()
    dst = str(target or "").strip()
    known = (src, dst)
    if src == LIVE_ENVIRONMENT and dst == SHADOW_ENVIRONMENT:
        return "live_to_shadow"
    if src == SHADOW_ENVIRONMENT and dst == LIVE_ENVIRONMENT:
        return FORBIDDEN_DIRECTION
    if src == SHADOW_ENVIRONMENT and dst == SHADOW_ENVIRONMENT:
        return "shadow_to_shadow"
    if src == LIVE_ENVIRONMENT and dst == LIVE_ENVIRONMENT:
        return "live_to_live"
    if src not in SHADOW_ENVIRONMENT_NAMES or dst not in known:
        return "unknown_direction"
    return "unknown_direction"


def feed_direction_verdict(feed: Mapping[str, Any]) -> dict[str, Any]:
    """One channel, checked against the direction table.

    Reports ``declared``, ``derived`` and ``permitted`` separately. When
    ``declared`` and ``derived`` disagree the row is an error even if the
    declared value was the permitted one, because a table that lies about its
    own direction is not a safety mechanism -- it is a comment that looks like one.
    """
    declared = str(feed.get("direction") or "")
    derived = derive_direction(feed.get("source"), feed.get("target"))
    row = DATA_DIRECTION_BY_NAME.get(derived)
    permitted = bool(row["permitted"]) if row is not None else False
    agrees = declared == derived
    reasons: list[str] = []
    if not agrees:
        reasons.append(f"declares {declared or '(none)'} but runs {derived}")
    if not permitted:
        reasons.append(
            f"{derived} is a forbidden direction; a shadow channel that reaches live "
            "writes to customers"
        )
    if row is None:
        reasons.append(f"{derived} is not a declared direction")
    return {
        "feed_id": str(feed.get("feed_id") or ""),
        "source": str(feed.get("source") or ""),
        "target": str(feed.get("target") or ""),
        "declared": declared,
        "derived": derived,
        "agrees": agrees,
        "permitted": permitted and agrees,
        "ok": permitted and agrees,
        "mode": str(feed.get("mode") or ""),
        "tables": list(_str_tuple(feed.get("tables"))),
        "reasons": reasons,
    }


def feed_direction_report(feeds: Any = None) -> dict[str, Any]:
    """Every channel, checked. The table's own summary, not a live probe."""
    rows = [dict(feed) for feed in (SHADOW_FEEDS if feeds is None else feeds)]
    verdicts = [feed_direction_verdict(feed) for feed in rows]
    failed = [verdict for verdict in verdicts if not verdict["ok"]]
    return {
        "generated_at": _now_iso(),
        "catalog_version": SHADOW_CATALOG_VERSION,
        "channels": len(verdicts),
        "forbidden_direction": FORBIDDEN_DIRECTION,
        "ok": not failed,
        "failed": [verdict["feed_id"] for verdict in failed],
        "verdicts": verdicts,
        "one_way": not failed,
    }


def leakage_paths(feeds: Any = None) -> list[str]:
    """Human-readable paths by which a shadow could reach live.

    Reported rather than blocked. The blocking is structural -- a shadow pointed
    at its own database cannot write live no matter what this function says --
    so the listing is evidence for the ``feed_direction`` check and for a human
    reading the catalog, not a second line of defence pretending to be the first.
    """
    rows = [dict(feed) for feed in (SHADOW_FEEDS if feeds is None else feeds)]
    return [
        f"{verdict['feed_id']}: {verdict['source']} -> {verdict['target']}"
        for verdict in (feed_direction_verdict(feed) for feed in rows)
        if not verdict["ok"]
    ]


def replicated_field_audit(feed: Mapping[str, Any]) -> dict[str, Any]:
    """Does a channel replicate a field that must never leave live?

    Belt-and-braces against :data:`NEVER_REPLICATED_FIELDS`, which is the check
    that survives a future edit to a ``scrub`` list. Substring matching, on
    purpose: the field names in this tree are ``hashed_password`` and
    ``api_key_hash``, and an exact-match check against ``password`` would wave
    both through.
    """
    scrubbed = [str(field).strip().lower() for field in _str_tuple(feed.get("scrub"))]
    tables = list(_str_tuple(feed.get("tables")))
    unscrubbed: list[str] = []
    for needle in NEVER_REPLICATED_FIELDS:
        if any(needle in field for field in scrubbed):
            continue
        unscrubbed.append(needle)
    return {
        "feed_id": str(feed.get("feed_id") or ""),
        "tables": tables,
        "scrub": sorted(scrubbed),
        "never_replicated_fields": list(NEVER_REPLICATED_FIELDS),
        "unscrubbed_sensitive_fields": unscrubbed,
        "note": (
            "unscrubbed_sensitive_fields lists the sensitive names no scrub entry "
            "covers; it is a field-name audit, not a column-level diff, and is "
            "advisory rather than blocking for that reason."
        ),
    }


# ---------------------------------------------------------------------------
# Observation


@dataclass
class Observation:
    """One measured fact, with absence recorded as absence.

    The alternative -- a dict of ``None`` values that a check treats as a pass --
    is how a guard becomes a rubber stamp: one refactor renames an input, the
    value goes missing, and every check reports green because nothing raised.
    """

    key: str
    value: Any = None
    measured: bool = False
    at: str = field(default_factory=_now_iso)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "measured": self.measured,
            "at": self.at,
            "detail": self.detail,
        }


def observed(key: str, value: Any, detail: str = "") -> Observation:
    """Record a measurement that was actually taken."""
    return Observation(key=str(key), value=value, measured=True, detail=str(detail))


def unobserved(key: str, detail: str = "") -> Observation:
    """Record a measurement that was *not* taken.

    A check that depends on this fails closed. That is the entire reason it is a
    distinct constructor rather than a default: "we did not look" must not be
    spellable as "we looked and it was fine".
    """
    return Observation(key=str(key), value=None, measured=False, detail=str(detail))


def _observation_value(
    observations: Mapping[str, Any], key: str
) -> tuple[Any, bool]:
    """Pull a measurement out of a mapping of any accepted shape.

    Accepts ``{key: value}``, ``{key: Observation}`` and ``{key: {"value":..}}``
    because the caller will be one of: a test, a CLI, or the router, and making
    each of them wrap its own value in a different type is how a guard ends up
    reading ``None`` from a measurement that was genuinely taken.
    """
    if key not in observations:
        return None, False
    raw = observations[key]
    if isinstance(raw, Observation):
        return raw.value, raw.measured
    if isinstance(raw, Mapping) and "value" in raw:
        return raw["value"], bool(raw.get("measured", True))
    return raw, True


# ---------------------------------------------------------------------------
# Isolation verdict


def _check_database_identity(observations: Mapping[str, Any]) -> dict[str, Any]:
    live_url, live_given = _observation_value(observations, "live_url")
    shadow_url, shadow_given = _observation_value(observations, "shadow_url")
    if not (live_given and shadow_given):
        missing = [
            name
            for name, given in (("live_url", live_given), ("shadow_url", shadow_given))
            if not given
        ]
        return {
            "passed": False,
            "detail": f"not measured: {', '.join(missing)}",
            "evidence": {"missing": missing},
        }
    live_identity = database_identity(live_url)
    shadow_identity = database_identity(shadow_url)
    identical = same_database(live_identity, shadow_identity)
    return {
        "passed": not identical,
        "detail": (
            "shadow and live resolve to the same database"
            if identical
            else f"shadow is on {shadow_identity.get('database') or '(unnamed)'} at "
            f"{shadow_identity.get('host')}"
        ),
        "evidence": {
            "live": {k: live_identity.get(k) for k in ("scheme", "host", "port", "database", "comparable")},
            "shadow": {k: shadow_identity.get(k) for k in ("scheme", "host", "port", "database", "comparable")},
            "identical": identical,
        },
    }


def _check_environment_marker(observations: Mapping[str, Any]) -> dict[str, Any]:
    value, measured = _observation_value(observations, "shadow_env_name")
    if not measured:
        return {"passed": False, "detail": "not measured", "evidence": {}}
    name = str(value or "").strip().lower()
    return {
        "passed": name == SHADOW_ENVIRONMENT,
        "detail": (
            f"marked {name or '(unset)'}"
            + (
                ""
                if name == SHADOW_ENVIRONMENT
                else f"; expected {SHADOW_ENVIRONMENT!r}, and a shadow process marked live "
                "is indistinguishable from production at the log level"
            )
        ),
        "evidence": {"shadow_env_name": name, "expected": SHADOW_ENVIRONMENT},
    }


def _check_write_scope(observations: Mapping[str, Any]) -> dict[str, Any]:
    value, measured = _observation_value(observations, "shadow_write_targets")
    if not measured:
        return {"passed": False, "detail": "not measured", "evidence": {}}
    targets = _str_tuple(value)
    live_targets = sorted(
        target
        for target in targets
        if target == "live_database" or target.startswith("live")
    )
    live_row = SHADOW_ENVIRONMENT_BY_NAME.get(SHADOW_ENVIRONMENT) or {}
    permitted = set(_str_tuple(live_row.get("writable_targets")))
    unexpected = sorted(set(targets) - permitted)
    reasons: list[str] = []
    if live_targets:
        reasons.append(f"declares live write targets: {', '.join(live_targets)}")
    if unexpected:
        reasons.append(f"declares targets outside the shadow's own scope: {', '.join(unexpected)}")
    return {
        "passed": not reasons,
        "detail": "; ".join(reasons) if reasons else f"writes only {', '.join(targets) or 'nothing'}",
        "evidence": {
            "declared": sorted(targets),
            "permitted": sorted(permitted),
            "live_targets": live_targets,
        },
    }


def _check_feed_direction(observations: Mapping[str, Any]) -> dict[str, Any]:
    value, measured = _observation_value(observations, "feeds")
    if not measured:
        return {"passed": False, "detail": "not measured", "evidence": {}}
    report = feed_direction_report(value)
    failed = [verdict for verdict in report["verdicts"] if not verdict["ok"]]
    return {
        "passed": report["one_way"],
        "detail": (
            f"{len(failed)} of {report['channels']} channels run the wrong way"
            if failed
            else f"all {report['channels']} channels are one-way"
        ),
        "evidence": {
            "channels": report["channels"],
            "failed": report["failed"],
            "leakage_paths": leakage_paths(value),
            "forbidden_direction": FORBIDDEN_DIRECTION,
        },
    }


def _check_live_read_only(observations: Mapping[str, Any]) -> dict[str, Any]:
    value, measured = _observation_value(observations, "live_read_only")
    if not measured:
        return {"passed": False, "detail": "not measured", "evidence": {}}
    return {
        "passed": bool(value),
        "detail": (
            "replication reaches live with a read-only principal"
            if value
            else "replication reaches live with a role that could write; the direction "
            "table is the only thing preventing it"
        ),
        "evidence": {"live_read_only": bool(value)},
    }


def _check_egress_allowlist(observations: Mapping[str, Any]) -> dict[str, Any]:
    value, measured = _observation_value(observations, "shadow_egress_allowlist")
    if not measured:
        return {"passed": False, "detail": "not measured", "evidence": {}}
    allowlist = _str_tuple(value)
    unrestricted = not allowlist
    return {
        "passed": not unrestricted,
        "detail": (
            "egress is unrestricted; a code change under test can reach anything the "
            "shadow host can"
            if unrestricted
            else f"egress limited to {', '.join(allowlist)}"
        ),
        "evidence": {"allowlist": list(allowlist)},
    }


_CHECK_IMPLEMENTATIONS = {
    "database_identity": _check_database_identity,
    "environment_marker": _check_environment_marker,
    "write_scope": _check_write_scope,
    "feed_direction": _check_feed_direction,
    "live_read_only": _check_live_read_only,
    "egress_allowlist": _check_egress_allowlist,
}


def evaluate_isolation(observations: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Is this shadow deployment unable to affect live? One verdict, no raising.

    Every row of :data:`ISOLATION_CHECKS` runs, including the advisory ones.
    Rolling up severity to a single ``isolated`` word is convenient for the
    release ladder and it is also lossy, so the per-check breakdown travels with
    it and ``advisories`` is reported separately from ``failures``. An advisory
    that fails is information about the deployment, not a reason to stop it.
    """
    given: Mapping[str, Any] = observations or {}
    checks: list[dict[str, Any]] = []
    for row in ISOLATION_CHECKS:
        check_id = str(row["check_id"])
        implementation = _CHECK_IMPLEMENTATIONS.get(check_id)
        if implementation is None:
            # A check added to the table with no implementation must fail, not be
            # skipped. Skipping would let a new row report as "isolated: true"
            # on the strength of having done nothing.
            checks.append(
                {
                    "check_id": check_id,
                    "severity": str(row["severity"]),
                    "asserts": str(row["asserts"]),
                    "passed": False,
                    "detail": "no implementation; an unrunnable check cannot pass",
                    "remedy": str(row["remedy"]),
                    "evidence": {},
                }
            )
            continue
        try:
            outcome = implementation(given)
        except Exception as exc:  # noqa: BLE001 - deliberately total
            # A check that raises has, in the only sense that matters here, not
            # passed. Letting the exception escape would turn a garbage
            # observation -- a string where a list belongs, a hand-edited
            # environment -- into a traceback instead of a verdict, and the
            # caller's response to a traceback is to read the stack rather than
            # to learn that the shadow is not isolated.
            checks.append(
                {
                    "check_id": check_id,
                    "severity": str(row["severity"]),
                    "asserts": str(row["asserts"]),
                    "passed": False,
                    "detail": f"could not be evaluated ({type(exc).__name__}: {exc})",
                    "remedy": str(row["remedy"]),
                    "evidence": {"raised": type(exc).__name__},
                }
            )
            continue
        checks.append(
            {
                "check_id": check_id,
                "severity": str(row["severity"]),
                "asserts": str(row["asserts"]),
                "passed": bool(outcome.get("passed", False)),
                "detail": str(outcome.get("detail") or ""),
                "remedy": str(row["remedy"]),
                "evidence": dict(outcome.get("evidence") or {}),
            }
        )

    blocking_failures = [
        check for check in checks if not check["passed"] and check["severity"] == "blocking"
    ]
    advisory_failures = [
        check for check in checks if not check["passed"] and check["severity"] == "advisory"
    ]
    return {
        "generated_at": _now_iso(),
        "catalog_version": SHADOW_CATALOG_VERSION,
        "isolated": not blocking_failures,
        "blocking_failures": [check["check_id"] for check in blocking_failures],
        "advisory_failures": [check["check_id"] for check in advisory_failures],
        "checks": checks,
        "checks_run": len(checks),
        "blocking_checks": sum(1 for check in checks if check["severity"] == "blocking"),
        "advisory_checks": sum(1 for check in checks if check["severity"] == "advisory"),
        "note": (
            "isolated is false when any blocking check failed or could not be measured. "
            "An unmeasured check fails closed: 'we did not look' is not 'we looked and it was fine'."
        ),
    }


def build_shadow_env_catalog(
    observations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The tables, their operators, and the live verdict -- the ``/kaizen`` shape."""
    return {
        "catalog_version": SHADOW_CATALOG_VERSION,
        "generated_at": _now_iso(),
        "environments": [dict(row) for row in SHADOW_ENVIRONMENTS],
        "directions": [dict(row) for row in DATA_DIRECTIONS],
        "checks": [dict(row) for row in ISOLATION_CHECKS],
        "feeds": [dict(row) for row in SHADOW_FEEDS],
        "feed_directions": feed_direction_report()["verdicts"],
        "field_audit": [replicated_field_audit(feed) for feed in SHADOW_FEEDS],
        "leakage_paths": leakage_paths(),
        "isolation": evaluate_isolation(observations),
        "operators": {
            "database_identity": "app.shadow_env.database_identity",
            "evaluate_isolation": "app.shadow_env.evaluate_isolation",
            "feed_direction_report": "app.shadow_env.feed_direction_report",
            "validate_shadow_env": "app.shadow_env.validate_shadow_env",
        },
    }


# ---------------------------------------------------------------------------
# Process configuration


def live_database_url() -> str:
    """The live database URL as this process sees it."""
    return str(os.getenv("DATABASE_URL") or "").strip()


def shadow_database_url() -> str:
    """The shadow database URL.

    Read from ``CSERVICE_SHADOW_DATABASE_URL`` and **not** from
    ``DATABASE_URL``. The asymmetry is deliberate and is the single most
    important line in this module: a shadow that inherited live's URL by
    fallback would be a live deployment wearing a shadow's name, and the only
    defence would be a convention somebody remembers.
    """
    return str(os.getenv("CSERVICE_SHADOW_DATABASE_URL") or "").strip()


def process_environment_name() -> str:
    """Which environment this process believes it is."""
    return str(os.getenv("CSERVICE_ENV") or os.getenv("APP_ENV") or "").strip().lower()


def build_process_observations(
    *,
    shadow_url: str | None = None,
    live_url: str | None = None,
    feeds: Iterable[Mapping[str, Any]] | None = None,
    shadow_env_name: str | None = None,
    shadow_write_targets: Any = None,
    live_read_only: Any = None,
    shadow_egress_allowlist: Any = None,
) -> dict[str, Observation]:
    """Measure this process for :func:`evaluate_isolation`.

    Only the checks this process can actually answer are *measured*; the rest
    are recorded as unobserved so they fail closed. A check that quietly
    reported a pass because nobody implemented its input would be exactly the
    "published number is not the enforced one" defect this repo has now found
    three times.

    ``shadow_url`` and ``live_url`` take overrides rather than only reading the
    environment, because the cases worth *testing* are the dangerous ones -- the
    same url on both sides, an unmarked shadow -- and a guard you can only
    exercise by editing a live process's environment is a guard nobody tests.
    An explicit ``""`` is honoured as "configured as empty" rather than falling
    back to the environment, so a caller can model an unconfigured shadow.
    """
    resolved_live_url = live_database_url() if live_url is None else str(live_url).strip()
    resolved_shadow_url = (
        shadow_database_url() if shadow_url is None else str(shadow_url).strip()
    )

    observations: dict[str, Observation] = {}

    if resolved_live_url:
        observations["live_url"] = observed("live_url", resolved_live_url, "from DATABASE_URL")
    else:
        observations["live_url"] = unobserved("live_url", "DATABASE_URL is unset")

    if resolved_shadow_url:
        observations["shadow_url"] = observed(
            "shadow_url", resolved_shadow_url, "from CSERVICE_SHADOW_DATABASE_URL"
        )
    else:
        observations["shadow_url"] = unobserved(
            "shadow_url",
            "CSERVICE_SHADOW_DATABASE_URL is unset; the shadow has not been pointed anywhere",
        )

    name = process_environment_name() if shadow_env_name is None else str(shadow_env_name)
    if name:
        observations["shadow_env_name"] = observed("shadow_env_name", name, "from CSERVICE_ENV")
    else:
        observations["shadow_env_name"] = unobserved("shadow_env_name", "CSERVICE_ENV is unset")

    targets = (
        _str_tuple(shadow_write_targets)
        if shadow_write_targets is not None
        else _str_tuple(os.getenv("CSERVICE_SHADOW_WRITE_TARGETS"))
    )
    observations["shadow_write_targets"] = (
        observed("shadow_write_targets", targets, "from CSERVICE_SHADOW_WRITE_TARGETS")
        if targets
        else unobserved("shadow_write_targets", "no shadow write targets declared")
    )

    observations["feeds"] = observed(
        "feeds", [dict(feed) for feed in (SHADOW_FEEDS if feeds is None else feeds)], "from SHADOW_FEEDS"
    )

    if live_read_only is not None:
        observations["live_read_only"] = observed("live_read_only", bool(live_read_only), "supplied by caller")
    elif os.getenv("CSERVICE_LIVE_READ_ONLY_REPLICATION"):
        raw = str(os.getenv("CSERVICE_LIVE_READ_ONLY_REPLICATION") or "")
        observations["live_read_only"] = observed("live_read_only", raw.strip().lower() in {"1", "true", "yes", "on"})
    else:
        observations["live_read_only"] = unobserved(
            "live_read_only",
            "set CSERVICE_LIVE_READ_ONLY_REPLICATION to record the replication role",
        )

    allowlist = (
        _str_tuple(shadow_egress_allowlist)
        if shadow_egress_allowlist is not None
        else _str_tuple(os.getenv("CSERVICE_SHADOW_EGRESS_ALLOWLIST"))
    )
    observations["shadow_egress_allowlist"] = (
        observed("shadow_egress_allowlist", allowlist, "from CSERVICE_SHADOW_EGRESS_ALLOWLIST")
        if allowlist
        else unobserved("shadow_egress_allowlist", "CSERVICE_SHADOW_EGRESS_ALLOWLIST is unset")
    )
    return observations


def current_isolation(
    **kwargs: Any,
) -> dict[str, Any]:
    """Evaluate this process's isolation from its own environment."""
    return evaluate_isolation(build_process_observations(**kwargs))


# ---------------------------------------------------------------------------
# Validation


def validate_shadow_env(
    observations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Errors are things that make a shadow unsafe; warnings are dead config.

    Split the way ``validate_complaints`` splits them, and for the same reason:
    a validator that returns one undifferentiated list is a list nobody reads,
    and the item that matters -- the shadow writes live -- is filed next to a
    cosmetic one and buried.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # --- the tables themselves
    seen_envs: set[str] = set()
    for index, row in enumerate(SHADOW_ENVIRONMENTS):
        name = str(row.get("environment") or "")
        if not name:
            errors.append(f"SHADOW_ENVIRONMENTS[{index}] has no environment name")
        elif name in seen_envs:
            errors.append(f"duplicate environment in SHADOW_ENVIRONMENTS: {name}")
        seen_envs.add(name)
        if not row.get("description"):
            warnings.append(f"SHADOW_ENVIRONMENTS[{index}] ({name}) has no description")

    for required in (LIVE_ENVIRONMENT, SHADOW_ENVIRONMENT):
        if required not in SHADOW_ENVIRONMENT_BY_NAME:
            errors.append(f"SHADOW_ENVIRONMENTS is missing the {required!r} environment")

    seen_checks: set[str] = set()
    for index, row in enumerate(ISOLATION_CHECKS):
        check_id = str(row.get("check_id") or "")
        if not check_id:
            errors.append(f"ISOLATION_CHECKS[{index}] has no check_id")
        elif check_id in seen_checks:
            errors.append(f"duplicate check_id in ISOLATION_CHECKS: {check_id}")
        seen_checks.add(check_id)
        severity = str(row.get("severity") or "")
        if severity not in {"blocking", "advisory"}:
            errors.append(f"ISOLATION_CHECKS[{check_id or index}] has severity {severity!r}")
        if check_id and check_id not in _CHECK_IMPLEMENTATIONS:
            errors.append(
                f"ISOLATION_CHECKS[{check_id}] has no implementation; a check that cannot run "
                "must not be counted as a pass"
            )
        if not row.get("remedy"):
            warnings.append(f"ISOLATION_CHECKS[{check_id or index}] has no remedy")

    # --- feed directions, and the sensitive-field audit
    # The feeds come from the *observation* when one was given, so both halves of
    # this report describe the same configuration. Reading the module table for
    # the directions and the observation for the isolation verdict would let the
    # report say "one-way" about seven channels and "not isolated" about a
    # different seven, and a reader would have to guess which was which.
    given_feeds = None
    given = observations or {}
    feed_observation = given.get("feeds") if hasattr(given, "get") else None
    if isinstance(feed_observation, Observation) and feed_observation.measured:
        given_feeds = feed_observation.value
    elif isinstance(feed_observation, (list, tuple)) and feed_observation:
        given_feeds = feed_observation

    directions = feed_direction_report(given_feeds)
    if not directions["one_way"]:
        for verdict in directions["verdicts"]:
            if verdict["ok"]:
                continue
            errors.append(
                f"SHADOW_FEEDS[{verdict['feed_id']}] {verdict['source']} -> {verdict['target']}: "
                + "; ".join(verdict["reasons"])
            )

    rows = SHADOW_FEEDS if given_feeds is None else [dict(feed) for feed in given_feeds]
    for feed in rows:
        audit = replicated_field_audit(feed)
        if audit["unscrubbed_sensitive_fields"]:
            warnings.append(
                f"SHADOW_FEEDS[{audit['feed_id']}] does not scrub any of "
                f"{', '.join(audit['unscrubbed_sensitive_fields'])}"
            )
        if not audit["tables"]:
            warnings.append(f"SHADOW_FEEDS[{audit['feed_id']}] replicates no tables")
        if not feed.get("description"):
            warnings.append(f"SHADOW_FEEDS[{audit['feed_id']}] has no description")

    seen_feeds: set[str] = set()
    for index, feed in enumerate(rows):
        feed_id = str(feed.get("feed_id") or "")
        if not feed_id:
            errors.append(f"SHADOW_FEEDS[{index}] has no feed_id")
        elif feed_id in seen_feeds:
            errors.append(f"duplicate feed_id in SHADOW_FEEDS: {feed_id}")
        seen_feeds.add(feed_id)

    # --- the live verdict, when we were given measurements
    #
    # Folded into `errors` only when an observation was actually supplied. Two
    # different questions are in play and conflating them makes the validator
    # useless in both directions: "are the tables self-consistent" is a property
    # of the code and must be answerable on a developer machine with no shadow
    # configured, while "is this deployment isolated" needs a measurement. An
    # earlier version folded the unmeasured verdict in unconditionally, so
    # `validate_shadow_env()` reported four errors on a clean checkout -- all of
    # them "not measured" -- and a reader could no longer tell a broken table
    # from an absent configuration. The verdict is still returned, and a caller
    # that *did* measure gets its failures promoted to errors, which is when
    # they are actionable.
    verdict = evaluate_isolation(observations)
    measured = bool(given)
    for check in verdict["checks"]:
        if check["passed"]:
            continue
        message = f"isolation check {check['check_id']} failed: {check['detail']}"
        if check["severity"] == "blocking" and measured:
            errors.append(message)
        elif check["severity"] == "blocking":
            warnings.append(f"{message} (no shadow configuration was measured)")
        else:
            warnings.append(
                f"isolation check {check['check_id']} is advisory and failed: {check['detail']}"
            )

    return {
        "generated_at": _now_iso(),
        "catalog_version": SHADOW_CATALOG_VERSION,
        "errors": errors,
        "warnings": warnings,
        "valid": not errors,
        "one_way": directions["one_way"],
        "isolated": verdict["isolated"],
        "checks_run": verdict["checks_run"],
        "environments": len(SHADOW_ENVIRONMENTS),
        "feeds": len(SHADOW_FEEDS),
        "leakage_paths": leakage_paths(given_feeds),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(SHADOW_FEEDS)} channels and {verdict['checks_run']} isolation checks"
        ),
    }
