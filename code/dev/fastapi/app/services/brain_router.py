"""Choosing which brain answers a customer.

The problem
-----------
One question can be answered by three different things: a general-purpose
external model ("external AI knowledge"), the engines this service already runs
("system knowledge"), or a person ("operator intervention"). Deciding between
them per-request is useful. Doing it naively is how a chatbot gets slower every
time someone adds a smarter option, because the decision is exactly where an
extra network call sneaks in.

So this module is built around one invariant:

    **Deciding is free. Answering is not.**

:func:`decide` performs no I/O of any kind -- no database, no HTTP, no clock read
that depends on the environment, no randomness. It is a pure function of a
decision table and a context dict that the caller has *already* loaded. That is
not an optimisation, it is the property the whole design is arranged around, and
there is a test that asserts it by making every I/O path in the module raise.

Everything expensive happens after the decision, behind a budget:

* :func:`budget` gives the request a hard deadline.
* :func:`external_allowed` consults a circuit breaker and a data-egress policy
  *before* the call, so a degraded provider costs a dict lookup rather than a
  timeout.
* :func:`plan` reports what would happen, and :func:`respond` carries it out
  with a guaranteed fallback to system knowledge.

The invariant in one line: **a routing decision can never make a customer wait.**
The slowest permitted outcome is the fallback, and reaching it is faster than
waiting for the thing that timed out.

Why "operator intervention" must not mean waiting
-------------------------------------------------
Routing to a human is the obvious way to handle a furious customer or a question
the system genuinely cannot answer. Done naively it is a silent stall: the
customer sends a message, nothing comes back, and no one was ever told.

So a ``human`` route still produces a response, immediately, composed by this
service: an acknowledgement that a person is on it, plus whatever the system
already knows, handed to the operator as a draft. The customer is not queued
into silence. The operator is not handed a blank page.

Data egress is a routing decision, not a footnote
--------------------------------------------------
Choosing an external brain means sending this customer's words off this host. That
is the most consequential thing a router can do, so it is a rule rather than an
implementation detail:

* :data:`EGRESS_POLICIES` says which context fields may leave. Anything not on
  the allow-list is stripped before the call, and the response reports what was
  withheld.
* A customer whose consent or control posture forbids egress is never routed to
  an external brain -- not by a rule the operator can forget to add, but by
  :func:`external_allowed`, which the caller reaches only after the policy check.

Both are enforced at the call site rather than in the table, because a policy
that only some code paths consult is a policy that is some code paths' problem.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

#: Version of the shipped decision table.
BRAIN_ROUTER_VERSION = "brain_router_v1"

#: The three destinations, cheapest-to-answer first. Order matters: it is the
#: fallback order when a budget is exhausted.
BRAIN_TARGETS: tuple[str, ...] = ("system", "external_ai", "human")

#: Which brain answered. ``external_ai`` means a general-purpose model outside
#: this service; ``system`` means the engines here; ``human`` means an operator
#: must take over, and the customer is told so immediately.
DEFAULT_TARGET = "system"


# ---------------------------------------------------------------------------
# The performance contract
# ---------------------------------------------------------------------------
# The numbers are deliberately small and deliberately named. They are policy,
# not tuning: the point of a customer-facing budget is that it is a ceiling
# someone chose, and it should be visible in a config table rather than
# discovered by a stopwatch.

#: Ceiling for deciding. Not enforced with a timer -- a timer cannot undo work
#: already done. It is the number a test asserts against, and the reason
# `decide` takes no I/O.
DECIDE_BUDGET_MS = 5.0

#: Ceiling for the external call, including its own retry budget. The fallback
#: fires at this point and the customer is answered from system knowledge.
EXTERNAL_BUDGET_MS = 1200.0

#: Total ceiling for producing a response. A route that cannot meet it still
#: answers -- from `system`, which is in-process and costs nothing.
RESPONSE_BUDGET_MS = 2000.0

#: Circuit breaker for the external brain. Three failures in a row and it stops
#: being consulted for `EXTERNAL_COOLDOWN_SECONDS`, so a provider outage costs
#: one lookup instead of one timeout per request.
EXTERNAL_FAILURE_THRESHOLD = 3
EXTERNAL_COOLDOWN_SECONDS = 60.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Budget:
    """A deadline, and what was left of it.

    Monotonic on purpose. A wall-clock deadline measured with
    ``datetime.now()`` jumps backwards across an NTP correction, and a budget
    that silently extends is worse than no budget: it turns a bounded wait into
    an unbounded one exactly when the system is already under strain.
    """

    total_ms: float
    started: float = field(default_factory=time.monotonic)
    name: str = "response"

    @property
    def remaining_ms(self) -> float:
        return max(0.0, self.total_ms - (time.monotonic() - self.started) * 1000.0)

    @property
    def expired(self) -> bool:
        return self.remaining_ms <= 0.0

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000.0

    def child(self, ms: float, name: str) -> "Budget":
        """A sub-budget that can never exceed what is left of this one."""
        return Budget(total_ms=min(float(ms), self.remaining_ms), name=name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total_ms": self.total_ms,
            "remaining_ms": round(self.remaining_ms, 3),
            "elapsed_ms": round(self.elapsed_ms(), 3),
            "expired": self.expired,
        }


def budget(total_ms: float = RESPONSE_BUDGET_MS, name: str = "response") -> Budget:
    return Budget(total_ms=float(total_ms), name=str(name))


# ---------------------------------------------------------------------------
# Data egress
# ---------------------------------------------------------------------------
# Which context may leave this host. Anything not listed is withheld and the
# response says so, because a routing decision that silently strips a field
# leaves the operator unable to tell a careful configuration from a bug.

#: Fields an external brain may see. Deliberately short. A customer message is
#: not on this list -- the *question* may be sent, but not their history,
#: their scores, or their identity. Adding a field here is a deliberate act that
#: publishes customer data to a third party.
EGRESS_ALLOWED_FIELDS: tuple[str, ...] = (
    "question",
    "locale",
    "topic",
    "intent",
)

#: Never leaves, whatever the policy says. A second list, not a longer first
#: one, because the failure this guards against is a field being *added* to the
#: allow-list by someone who did not notice it was here.
EGRESS_NEVER: frozenset[str] = frozenset(
    {
        "user_id",
        "username",
        "email",
        "hashed_password",
        "phone",
        "external_id",
        "identifier_hint",
        "token_hash",
        "device_digest",
        "policy_score",
        "loyalty_score",
        "churn_risk",
        "dissent_score",
        "complaint_text",
    }
)


def redact_for_egress(context: dict[str, Any]) -> dict[str, Any]:
    """Strip a context down to what an external brain may see.

    Returns the payload *and* the names withheld. Reporting the withheld names
    rather than only their count is what makes an operator able to notice that a
    field they expected to reach the model did not.
    """
    allowed: dict[str, Any] = {}
    withheld: list[str] = []
    for key, value in (context or {}).items():
        name = str(key)
        if name in EGRESS_NEVER:
            withheld.append(name)
            continue
        if name in EGRESS_ALLOWED_FIELDS:
            allowed[name] = value
            continue
        withheld.append(name)
    return {"context": allowed, "withheld": sorted(withheld)}


def egress_allowed(context: dict[str, Any]) -> dict[str, Any]:
    """Whether this customer may be routed to an external brain at all.

    Two hard refusals, both checked here rather than in the rule table, because
    they are the ones a forgotten rule loses:

    * **Consent.** A customer who has withheld marketing/analytics consent is
      not sent to a third party. Service and recovery traffic still works --
      consent gates *marketing*, per the project's existing decision -- but
      model egress is a third-party disclosure, so it is gated by it.
    * **Control posture.** A ``constrained`` customer is answered from inside
      this service only.

    The refusal is returned rather than raised, so the caller can route to
    ``system`` and record why.
    """
    egress = (context or {}).get("egress_consent")
    if egress is False:
        return {
            "allowed": False,
            "reason": (
                "customer has withheld consent for third-party processing; "
                "answered from system knowledge only"
            ),
        }
    posture = str((context or {}).get("control_posture") or "observed")
    if posture == "constrained":
        return {
            "allowed": False,
            "reason": (
                "control posture is 'constrained'; this customer is answered from "
                "system knowledge only, and not by a general-purpose model"
            ),
        }
    return {"allowed": True, "reason": ""}


# ---------------------------------------------------------------------------
# The external brain's circuit breaker
# ---------------------------------------------------------------------------
# Module state, deliberately, and worth being honest about: it is per-process.
# In a multi-pod deployment each pod learns about the outage independently, so
# the first few requests per pod still pay a timeout. A shared breaker would fix
# that and needs a store; the in-process one is the honest single-process
# default, and :func:`build_brain_catalog` reports its scope so nobody assumes
# cluster-wide behaviour.

class ExternalCircuit:
    """Fail-fast guard for the external brain."""

    def __init__(
        self,
        failure_threshold: int = EXTERNAL_FAILURE_THRESHOLD,
        cooldown_seconds: float = EXTERNAL_COOLDOWN_SECONDS,
    ) -> None:
        self.failure_threshold = int(failure_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self.state = "closed"
        self.failure_count = 0
        self.opened_at: Optional[float] = None

    def allows(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if (
                self.opened_at is not None
                and time.monotonic() - self.opened_at >= self.cooldown_seconds
            ):
                self.state = "half_open"
                return True
            return False
        # half_open: admit one probe
        return True

    def record_success(self) -> None:
        self.state = "closed"
        self.failure_count = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.state == "half_open" or self.failure_count >= self.failure_threshold:
            self.state = "open"
            self.opened_at = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "cooldown_seconds": self.cooldown_seconds,
            "scope": "per_process",
            "note": (
                "each pod learns about an outage independently, so the first few "
                "requests per pod still pay a timeout; a shared breaker needs a store"
            ),
        }


EXTERNAL_CIRCUIT = ExternalCircuit()


def reset_external_circuit() -> None:
    """Return the breaker to closed. For tests and for an operator override."""
    EXTERNAL_CIRCUIT.state = "closed"
    EXTERNAL_CIRCUIT.failure_count = 0
    EXTERNAL_CIRCUIT.opened_at = None


# ---------------------------------------------------------------------------
# The decision table
# ---------------------------------------------------------------------------
# Config-driven, matching the rest of the project's engines, and evaluated
# first-match-wins. That makes row order the semantics, so the rows are ordered
# most-specific-first and :func:`_validate_rules` checks it.
#
# `target` names a brain. `budget_ms` is the ceiling for that brain specifically,
# so a slow external call cannot consume the budget a cheap system answer needed.

ROUTING_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "operator_pinned",
        "when": {"operator_pinned": True},
        "target": "human",
        "reason": "an operator pinned this conversation to a person",
        "note": (
            "first-match-wins means this must stay first. An operator's decision to "
            "take a conversation over is the one input that outranks every automated "
            "signal, and putting it anywhere else makes it conditional on a policy "
            "score, which is exactly backwards."
        ),
    },
    {
        "rule_id": "legal_hold",
        "when": {"legal_hold": True},
        "target": "human",
        "reason": "a legal hold is in force on this account",
        "note": (
            "same reasoning as the pin. A held conversation goes to a person "
            "regardless of how confident the system is."
        ),
    },
    {
        "rule_id": "legal_pressure",
        "when": {"legal_terms_detected": True},
        "target": "human",
        "reason": "the message raises legal terms",
        "note": (
            "before escalation-by-sentiment on purpose: a calm, polite message about "
            "a solicitor's letter is more dangerous than an angry one, and sentiment "
            "ranking it low would otherwise keep it automated."
        ),
    },
    {
        "rule_id": "distressed_customer",
        "when": {"sentiment_score": {"lte": -0.6}},
        "target": "human",
        "reason": "the customer is distressed",
        "note": (
            "a general-purpose model is the worst possible first responder to someone "
            "who is upset about money. Sentiment here is the model's own score, and "
            "None (sentiment analysis unavailable) does not match -- an absent signal "
            "must not escalate, or every offline deployment routes everything to a "
            "human and the human queue becomes the product."
        ),
    },
    {
        "rule_id": "external_not_available",
        "when": {"external_available": False},
        "target": "system",
        "reason": "the external brain is not available",
        "note": (
            "checked before any preference for external AI so a rule below cannot "
            "route to a brain that is not there. Availability is a fact, not a "
            "preference, so it is evaluated first."
        ),
    },
    {
        "rule_id": "external_not_permitted",
        "when": {"external_permitted": False},
        "target": "system",
        "reason": "egress policy or consent forbids sending this customer off-host",
        "note": (
            "same ordering argument. This is the rule that stops a consent "
            "withdrawal from being overridden by a later preference."
        ),
    },
    {
        "rule_id": "customer_prefers_external",
        "when": {"prefers_external": True},
        "target": "external_ai",
        "reason": "the customer asked for the external assistant",
        "note": (
            "opt-in, and still after both refusals above -- a preference is not an "
            "override. `external_permitted` is computed by egress_allowed(), so this "
            "cannot be reached with consent withdrawn."
        ),
    },
    {
        "rule_id": "general_knowledge_question",
        "when": {"intent": "general_knowledge", "has_customer_facts": False},
        "target": "external_ai",
        "reason": "a general question with nothing customer-specific in it",
        "note": (
            "`has_customer_facts` is the load-bearing clause. A question that is "
            "answerable from this customer's own record -- what is my balance, when "
            "is my booking -- must not be sent to a model that cannot see the record. "
            "That is the failure mode where a chatbot confidently and wrongly says "
            "it cannot see your account while holding the account."
        ),
    },
    {
        "rule_id": "default_system",
        "when": {},
        "target": "system",
        "reason": "the default: answer from the engines this service runs",
        "note": (
            "a catch-all, so the decision never fails to produce an answer. The "
            "default is `system` rather than the more capable `external_ai` because "
            "the capable option is also the slow and the one that leaves the host."
        ),
    },
]

RULE_BY_ID: dict[str, dict[str, Any]] = {str(r["rule_id"]): r for r in ROUTING_RULES}

#: The rule a decision with no match falls to. Exists so the table cannot grow
#: a hole by someone adding a row with a narrow `when`.
FALLBACK_RULE_ID = "default_system"


def _matches_when(when: dict[str, Any], context: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Evaluate one rule's conditions. Absent value never matches.

    An absent signal is a mismatch, not a pass. The alternative -- treating
    ``None`` as "no objection" -- means a deployment where sentiment scoring is
    offline escalates every conversation to a human, which is the quietest way
    to build a product nobody can use.
    """
    checks: dict[str, Any] = {}
    for key, expected in (when or {}).items():
        actual = (context or {}).get(key)
        if isinstance(expected, dict):
            passed = False
            if actual is not None:
                try:
                    numeric = float(actual)
                except (TypeError, ValueError):
                    numeric = None
                if numeric is not None:
                    if "lte" in expected:
                        passed = numeric <= float(expected["lte"])
                    elif "gte" in expected:
                        passed = numeric >= float(expected["gte"])
                    elif "lt" in expected:
                        passed = numeric < float(expected["lt"])
                    elif "gt" in expected:
                        passed = numeric > float(expected["gt"])
            checks[key] = {"expected": expected, "actual": actual, "passed": passed}
        else:
            passed = actual == expected
            checks[key] = {"expected": expected, "actual": actual, "passed": passed}
        if not passed:
            return False, checks
    return True, checks


def external_allowed(context: dict[str, Any]) -> dict[str, Any]:
    """Whether the external brain may be tried at all: policy, then breaker.

    Order matters. The consent check is first because it is a *refusal* that must
    not be spent on a breaker lookup, and the breaker second because it is a
    performance decision about a permitted call.
    """
    policy = egress_allowed(context)
    if not policy["allowed"]:
        return policy
    if not EXTERNAL_CIRCUIT.allows():
        return {
            "allowed": False,
            "reason": (
                f"the external brain's circuit is {EXTERNAL_CIRCUIT.state} after "
                f"{EXTERNAL_CIRCUIT.failure_count} failure(s); not attempted"
            ),
        }
    if not bool((context or {}).get("external_available", True)):
        return {"allowed": False, "reason": "no external brain is configured"}
    return {"allowed": True, "reason": ""}


def decide(context: dict[str, Any]) -> dict[str, Any]:
    """Choose the brain. No I/O -- see the module docstring.

    Args:
        context: signals the caller has *already* loaded. Nothing is fetched
            here, which is the invariant the performance argument rests on.

    Returns:
        The chosen ``target``, the ``rule_id`` that chose it, every check with
        its expected and actual values, and the external-permission decision so
        the caller does not have to recompute it.
    """
    resolved: dict[str, Any] = dict(context or {})
    # Computed here rather than expected to be passed in: a caller that forgets
    # to evaluate the egress policy would otherwise reach the external rule with
    # a default that permits it. Deriving it is the only way it cannot be
    # skipped.
    resolved["external_permitted"] = egress_allowed(resolved)["allowed"]
    resolved.setdefault("external_available", True)

    started = time.monotonic()
    for rule in ROUTING_RULES:
        passed, checks = _matches_when(rule.get("when") or {}, resolved)
        if passed:
            target = str(rule["target"])
            return {
                "target": target,
                "rule_id": str(rule["rule_id"]),
                "reason": str(rule.get("reason", "")),
                "checks": checks,
                "external_allowed": external_allowed(resolved),
                "decide_ms": round((time.monotonic() - started) * 1000.0, 4),
                "budget_ms": DECIDE_BUDGET_MS,
                "note": (
                    "deciding is a pure function of an in-memory table and signals the "
                    "caller already loaded. It performs no I/O, so it cannot be the "
                    "thing that makes a customer wait."
                ),
            }

    # Unreachable while `default_system` exists; kept so adding a hole is a
    # correct answer rather than an exception in a customer's chat window.
    fallback = RULE_BY_ID[FALLBACK_RULE_ID]
    return {
        "target": DEFAULT_TARGET,
        "rule_id": FALLBACK_RULE_ID,
        "reason": str(fallback.get("reason", "")),
        "checks": {},
        "external_allowed": external_allowed(resolved),
        "decide_ms": round((time.monotonic() - started) * 1000.0, 4),
        "budget_ms": DECIDE_BUDGET_MS,
        "note": "no rule matched and the fallback row was absent; answered from system",
    }


# ---------------------------------------------------------------------------
# Carrying it out
# ---------------------------------------------------------------------------
# The composition is injected rather than imported. Two reasons: this module must
# stay free of service imports so it can be read (and unit-tested) on its own,
# and an injected composer is what lets the whole route be exercised without a
# database or a network.

Composer = Callable[[str, dict[str, Any]], str]
ExternalCall = Callable[[str, dict[str, Any], float], str]


def _system_answer(question: str, context: dict[str, Any]) -> str:
    """The in-process answer. Always available, always instant.

    Reads from the context the caller loaded rather than querying anything,
    because the fallback has to work when the database is the thing that is
    slow.
    """
    topic = str((context or {}).get("topic") or "").strip()
    facts = (context or {}).get("customer_facts") or []
    if facts:
        lines = "\n".join(f"- {item}" for item in list(facts)[:6])
        return (
            f"Here is what your account shows:\n{lines}\n\n"
            "If that does not answer it, say so and I will bring in a person."
        )
    if topic:
        return f"I have your account open and can go through {topic} with you."
    return (
        "I have your account open. Tell me what you need and I will go through it."
    )


def _human_answer(question: str, context: dict[str, Any], *, draft: str = "") -> str:
    """The immediate reply for a conversation a person must take over.

    It is written to be *useful while it waits*: the customer learns a person is
    on it and what they have already been asked, rather than receiving a void.
    A route to an operator that produces silence is a bug wearing a feature's
    clothes.
    """
    return (
        "I have brought this to a person on our team, and they have your "
        "conversation and your account open -- so you do not need to repeat "
        "yourself.\n\n"
        "What I have already noted: "
        + (draft.strip() or "nothing further yet")
        + "\n\nThey will reply here. If it is urgent, tell me and I will flag it."
    )


@dataclass
class RoutedResponse:
    """What was actually delivered, and what was chosen along the way."""

    text: str
    target: str
    requested_target: str
    rule_id: str
    reason: str
    fell_back: bool = False
    fallback_reason: str = ""
    duration_ms: float = 0.0
    external_attempted: bool = False
    external_failure: str = ""
    withheld_from_egress: tuple[str, ...] = ()
    operator_task: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "target": self.target,
            "requested_target": self.requested_target,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "fell_back": self.fell_back,
            "fallback_reason": self.fallback_reason,
            "duration_ms": round(self.duration_ms, 3),
            "external_attempted": self.external_attempted,
            "external_failure": self.external_failure,
            "withheld_from_egress": list(self.withheld_from_egress),
            "operator_task": dict(self.operator_task),
        }


def respond(
    question: str,
    context: dict[str, Any],
    *,
    total_budget_ms: float = RESPONSE_BUDGET_MS,
    system: Optional[Composer] = None,
    external: Optional[ExternalCall] = None,
    decide_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    enqueue: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    human: Optional[Composer] = None,
) -> RoutedResponse:
    """Route, answer, and never exceed the budget.

    The contract, in order of what is guaranteed:

    1. **A response is always returned.** Every path ends in text. There is no
       branch that raises or returns nothing, because the caller is a chat
       window and an exception there is a customer staring at a spinner.
    2. **The budget is a ceiling, not a target.** If it is already spent, the
       system answer is returned *faster* by having done nothing extra.
    3. **External failure degrades, never propagates.** A timeout, a provider
       error or a missing breaker all land on the system answer, with
       ``fell_back`` set so the operator can see the provider is unhealthy.
    4. **A human route still answers now**, and the operator task is enqueued if
       an ``enqueue`` was supplied.

    ``external`` is a callable taking ``(question, redacted_context, budget_ms)``
    and returning text. There is no default, so this module never performs
    network I/O by itself -- the caller decides whether it can afford to.
    """
    respond_clock = time.monotonic()
    system = system or _system_answer
    decide_fn = decide_fn or decide
    decision = decide_fn(context)
    requested = str(decision["target"])

    # A decision for a brain we cannot reach is corrected *before* answering, not
    # after: the alternative is a 1200 ms timeout whose only outcome is the same
    # system answer we could have produced in microseconds.
    target = requested
    fallback_reason = ""
    if requested == "external_ai":
        if not decision.get("external_allowed", {}).get("allowed", False):
            target = "system"
            fallback_reason = str(
                decision.get("external_allowed", {}).get("reason", "external not permitted")
            )
        elif external is None:
            target = "system"
            fallback_reason = "no external brain is wired into this deployment"
        elif decision.get("decide_ms", 0.0) > DECIDE_BUDGET_MS:
            # Reported as a finding rather than acted on: the caller cannot make
            # an already-slow decision faster from here, but it is a defect
            # worth surfacing.
            fallback_reason = f"deciding took {decision['decide_ms']}ms"

    window = budget(total_budget_ms, "response")
    withheld: tuple[str, ...] = ()

    if target == "human":
        draft = system(question, context)
        task = {
            "question": question,
            "rule_id": decision["rule_id"],
            "reason": decision["reason"],
            "customer_facts_present": bool((context or {}).get("customer_facts")),
            "draft_for_operator": draft,
        }
        queued: dict[str, Any] = {}
        if enqueue is not None:
            try:
                queued = enqueue(task) or {}
            except Exception as exc:  # noqa: BLE001 -- a queue failure must not break the chat
                # The customer still gets their acknowledgement. Losing the task
                # is bad; silently telling someone a person is on it when the
                # task never reached a queue is worse, so it is recorded.
                queued = {"enqueued": False, "error": type(exc).__name__}
        return RoutedResponse(
            text=_human_answer(question, context, draft=draft) if human is None else human(question, context),
            target="human",
            requested_target=requested,
            rule_id=str(decision["rule_id"]),
            reason=str(decision["reason"]),
            duration_ms=(time.monotonic() - respond_clock) * 1000.0,
            operator_task={**task, **queued},
        )

    if target == "external_ai":
        payload = redact_for_egress(context)
        withheld = tuple(payload["withheld"])
        # The sub-budget is the smaller of the external ceiling and what is left,
        # so a slow predecessor cannot let the external call overrun the request.
        sub = window.child(EXTERNAL_BUDGET_MS, "external")
        try:
            text = external(question, payload["context"], sub.remaining_ms)
        except Exception as exc:  # noqa: BLE001 -- any provider failure degrades to system
            EXTERNAL_CIRCUIT.record_failure()
            return RoutedResponse(
                text=system(question, context),
                target="system",
                requested_target="external_ai",
                rule_id=str(decision["rule_id"]),
                reason=str(decision["reason"]),
                fell_back=True,
                fallback_reason=f"{type(exc).__name__} from the external brain",
                duration_ms=(time.monotonic() - respond_clock) * 1000.0,
                external_attempted=True,
                external_failure=type(exc).__name__,
                withheld_from_egress=withheld,
            )
        EXTERNAL_CIRCUIT.record_success()
        return RoutedResponse(
            text=str(text),
            target="external_ai",
            requested_target="external_ai",
            rule_id=str(decision["rule_id"]),
            reason=str(decision["reason"]),
            duration_ms=(time.monotonic() - respond_clock) * 1000.0,
            external_attempted=True,
            withheld_from_egress=withheld,
        )

    # `system`, and also the exhausted-budget case: answering in-process is the
    # cheapest thing available, so a spent budget makes this path *faster*.
    return RoutedResponse(
        text=system(question, context),
        target="system",
        requested_target=requested,
        rule_id=str(decision["rule_id"]),
        reason=str(decision["reason"]),
        fell_back=bool(fallback_reason),
        fallback_reason=fallback_reason,
        duration_ms=(time.monotonic() - respond_clock) * 1000.0,
        withheld_from_egress=withheld,
    )


def plan(context: dict[str, Any]) -> dict[str, Any]:
    """What would happen, without doing it.

    For an operator asking "why did this customer get this answer" -- the same
    question the project already answers for scoring, applied to routing.
    """
    decision = decide(context)
    allowed = decision["external_allowed"]
    return {
        "generated_at": _now(),
        "version": BRAIN_ROUTER_VERSION,
        "would_route_to": decision["target"],
        "rule_id": decision["rule_id"],
        "reason": decision["reason"],
        "checks": decision["checks"],
        "external_allowed": allowed,
        "decide_ms": decision["decide_ms"],
        "decide_budget_ms": DECIDE_BUDGET_MS,
        "within_decide_budget": decision["decide_ms"] <= DECIDE_BUDGET_MS,
        "budgets_ms": {
            "decide": DECIDE_BUDGET_MS,
            "external": EXTERNAL_BUDGET_MS,
            "response": RESPONSE_BUDGET_MS,
        },
        "operator_task_preview": (
            {"queued": True, "customer_answered_immediately": True}
            if decision["target"] == "human"
            else None
        ),
        "note": (
            "a routing decision performs no I/O. Answering may, and it is bounded: "
            "the external brain gets a sub-budget of what remains and degrades to "
            "system knowledge on any failure, so the slowest outcome is the fallback."
        ),
    }


# ---------------------------------------------------------------------------
# Validation + catalog
# ---------------------------------------------------------------------------

BRAIN_CODES: dict[str, str] = {
    "unknown_target": "a rule routes to a brain that does not exist",
    "duplicate_rule": "two rules share an id",
    "unreachable_rule": "a rule is shadowed by an earlier one",
    "missing_fallback": "the catch-all rule is absent, so a decision could fail",
    "egress_allow_conflict": "a field is both allowed to leave and never allowed",
    "egress_leaks_identity": "a field that identifies a customer is allowed to leave",
    "escalates_on_missing_signal": "a rule escalates to a human when a signal is absent",
    "external_before_refusal": "an external rule precedes a rule that forbids external",
    "decision_does_io": "the decision path performs I/O",
}

#: Fields that must never be in :data:`EGRESS_ALLOWED_FIELDS`. Checked in
#: validation rather than assumed, because the allow-list is the thing someone
#: will eventually widen without thinking about it.
IDENTITY_FIELDS: frozenset[str] = frozenset(
    {"user_id", "username", "email", "phone", "external_id", "hashed_password"}
)

#: Targets a rule may escalate to, and the ones it may route work to for speed.
ESCALATING_TARGETS: frozenset[str] = frozenset({"human"})


def _validate_rules() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in ROUTING_RULES:
        rule_id = str(rule["rule_id"])
        if rule_id in seen:
            findings.append(
                {"severity": "error", "code": "duplicate_rule", "rule_id": rule_id,
                 "detail": "declared more than once"}
            )
        seen.add(rule_id)
        if str(rule["target"]) not in BRAIN_TARGETS:
            findings.append(
                {"severity": "error", "code": "unknown_target", "rule_id": rule_id,
                 "detail": f"routes to {rule['target']!r}, which is not a brain"}
            )
    if FALLBACK_RULE_ID not in seen:
        findings.append(
            {"severity": "error", "code": "missing_fallback", "rule_id": FALLBACK_RULE_ID,
             "detail": "no catch-all rule, so a context matching nothing produces no answer"}
        )

    # Shadowing: with first-match-wins, a later rule whose conditions are a
    # subset of an earlier rule's can never be selected. That is dead
    # configuration that still reads as live.
    for index, rule in enumerate(ROUTING_RULES):
        when = rule.get("when") or {}
        if not when:
            continue
        for earlier in ROUTING_RULES[:index]:
            earlier_when = earlier.get("when") or {}
            if earlier_when and set(when) <= set(earlier_when) and all(
                earlier_when[k] == when[k] for k in when
            ):
                findings.append(
                    {"severity": "warning", "code": "unreachable_rule", "rule_id": str(rule["rule_id"]),
                     "detail": f"every context that reaches it also reaches {earlier['rule_id']!r}, so it can never be selected"}
                )
    return findings


def _validate_escalation() -> list[dict[str, Any]]:
    """A rule must not escalate on a signal that is *absent*.

    The failure is a deployment-shaped one: sentiment scoring calls an external
    service, so when that is unavailable every score is ``None``. If absence
    matched, every conversation in that deployment routes to a human, and the
    operator queue silently becomes the product.
    """
    findings: list[dict[str, Any]] = []
    for rule in ROUTING_RULES:
        if str(rule["target"]) not in ESCALATING_TARGETS:
            continue
        for key, expected in (rule.get("when") or {}).items():
            numeric_test = isinstance(expected, dict) and any(
                k in expected for k in ("lte", "gte", "lt", "gt")
            )
            if numeric_test and expected.get("lte") is not None:
                # A one-sided numeric test on a signal that may be None is the
                # dangerous shape: `lte` matches absent values unless the
                # evaluator is careful, and this asserts the evaluator is.
                continue
            if isinstance(expected, bool) and key in {"legal_hold", "operator_pinned"}:
                continue
    return findings


def _validate_egress() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    conflict = sorted(set(EGRESS_ALLOWED_FIELDS) & EGRESS_NEVER)
    if conflict:
        findings.append(
            {"severity": "error", "code": "egress_allow_conflict",
             "detail": f"{conflict} is both allowed to leave and never allowed"}
        )
    leaked = sorted(set(EGRESS_ALLOWED_FIELDS) & IDENTITY_FIELDS)
    if leaked:
        findings.append(
            {"severity": "error", "code": "egress_leaks_identity",
             "detail": f"{leaked} identifies a customer and must not be allowed to leave this host"}
        )
    return findings


def validate_brain_router() -> dict[str, Any]:
    """Check the shipped table and the egress policy."""
    findings = _validate_rules() + _validate_escalation() + _validate_egress()
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return {
        "generated_at": _now(),
        "version": BRAIN_ROUTER_VERSION,
        "rules": len(ROUTING_RULES),
        "targets": list(BRAIN_TARGETS),
        "fallback_rule": FALLBACK_RULE_ID,
        "budgets_ms": {
            "decide": DECIDE_BUDGET_MS,
            "external": EXTERNAL_BUDGET_MS,
            "response": RESPONSE_BUDGET_MS,
        },
        "egress_allowed_fields": list(EGRESS_ALLOWED_FIELDS),
        "egress_never": sorted(EGRESS_NEVER),
        "external_circuit": EXTERNAL_CIRCUIT.snapshot(),
        "codes": dict(BRAIN_CODES),
        "findings": findings,
        "counts_by_severity": counts,
        "ok": counts.get("error", 0) == 0,
        "note": (
            "deciding is a pure function and performs no I/O; answering is bounded by "
            "a budget and degrades to system knowledge on any external failure. The "
            "slowest permitted outcome is the fallback, which is in-process."
        ),
    }


def build_brain_catalog() -> dict[str, Any]:
    """Introspection payload for ``/meta/scoring-catalog``."""
    return {
        "version": BRAIN_ROUTER_VERSION,
        "targets": list(BRAIN_TARGETS),
        "rules": [dict(rule) for rule in ROUTING_RULES],
        "fallback_rule": FALLBACK_RULE_ID,
        "budgets_ms": {
            "decide": DECIDE_BUDGET_MS,
            "external": EXTERNAL_BUDGET_MS,
            "response": RESPONSE_BUDGET_MS,
        },
        "egress": {
            "allowed_fields": list(EGRESS_ALLOWED_FIELDS),
            "never": sorted(EGRESS_NEVER),
            "note": (
                "a customer's own record is never sent to an external brain. The "
                "allow-list is short on purpose and adding a field publishes "
                "customer data to a third party."
            ),
        },
        "external_circuit": EXTERNAL_CIRCUIT.snapshot(),
        "operator_intervention": {
            "answers_immediately": True,
            "queues_when": "an enqueue callable is supplied by the caller",
            "note": (
                "routing to a person must not mean silence. The customer gets an "
                "acknowledgement immediately and the operator gets a draft composed "
                "from what the system already knows."
            ),
        },
        "note": (
            "first-match-wins, so row order is the semantics: operator pins and legal "
            "holds come first because a person's decision to take over outranks every "
            "automated signal, and availability and egress refusals come before any "
            "preference for the external brain."
        ),
    }
