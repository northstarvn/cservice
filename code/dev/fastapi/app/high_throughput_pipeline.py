"""High-throughput audit/security signal pipeline.

The audit domain becomes async-friendly: request paths enqueue a security
signal in microseconds and a small pool of workers drains the queue in
batches, flushing each batch to a pluggable sink. The default sink encodes
every event as an immutable, hash-chained protobuf transaction
(``app.protobuf_transaction_spec``), so the audit trail stays append-only even
under load.

Design (config-driven, non-rigid):

- ``PIPELINE_WORKERS`` / ``PIPELINE_BATCH_SIZE`` / ``PIPELINE_MAX_QUEUE`` /
  ``PIPELINE_FLUSH_SECONDS`` — env-tunable throughput knobs.
- ``CSERVICE_PIPELINE_AUTOSTART`` — the app only spins up workers when this is
  set (defaults off, keeping the test suite and existing behavior untouched);
  ``submit`` still accepts events without workers, and ``drain`` flushes them.
- Backpressure: bounded queue; ``submit`` returns ``None`` (caller sees 503)
  when the queue is full instead of blocking request threads.
- Dead-lettering: a failed batch is counted and its events are retained
  (bounded ring) for inspection instead of being silently lost.

Expansion (thin-group pass): the original knobs only answer *how fast* to
drain, not *what* should drain. These config tables answer that, so changing
delivery policy is data rather than a code change:

- ``PIPELINE_ROUTES`` — kind prefix -> route (``security``/``billing``/
  ``data``/...): the route sets the default severity floor, the queue priority,
  and whether a dead letter from it may be replayed. An unrecognised kind falls
  into ``default`` rather than being dropped.
- ``SEVERITY_PRIORITY`` — severity -> queue priority. Priority decides who
  drains first when a batch deadline expires with events still queued.
- ``SAMPLING_RULES`` — per route/severity keep-rate. A 40x amplification on
  low-severity data events is usually noise; a critical security event is
  never sampled away. ``0.0`` never samples, ``1.0`` always keeps.
- ``ADMISSION_LIMITS`` — per-tenant in-flight cap, so one noisy tenant cannot
  consume the whole bounded queue and starve everyone else.
- ``REPLAY_POLICY`` — bounded dead-letter replay with exponential backoff.
- ``fanout_sink`` — compose several sinks; one failing sink never takes the
  batch down with it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.protobuf_transaction_spec import TransactionLog, get_default_transaction_log

logger = logging.getLogger(__name__)

PIPELINE_WORKERS = int(os.getenv("PIPELINE_WORKERS", "2"))
PIPELINE_BATCH_SIZE = int(os.getenv("PIPELINE_BATCH_SIZE", "50"))
PIPELINE_MAX_QUEUE = int(os.getenv("PIPELINE_MAX_QUEUE", "2000"))
PIPELINE_FLUSH_SECONDS = float(os.getenv("PIPELINE_FLUSH_SECONDS", "1.0"))
PIPELINE_AUTOSTART = os.getenv("CSERVICE_PIPELINE_AUTOSTART", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEAD_LETTER_MAX = int(os.getenv("PIPELINE_DEAD_LETTER_MAX", "500"))
REPLAY_ATTEMPTS = int(os.getenv("PIPELINE_REPLAY_ATTEMPTS", "3"))
REPLAY_BACKOFF_SECONDS = float(os.getenv("PIPELINE_REPLAY_BACKOFF_SECONDS", "0.5"))

# --- Expansion: delivery-policy config ---------------------------------------

# severity -> queue priority. Lower runs first.
SEVERITY_PRIORITY: dict[str, int] = {
    "critical": 0,
    "warning": 1,
    "info": 2,
}
DEFAULT_SEVERITY = "info"

# kind prefix -> route config. Longest prefix wins; `default` is the fallback.
PIPELINE_ROUTES: list[dict[str, Any]] = [
    {
        "route": "security",
        "match": "auth.",
        "label": "Authentication & session security",
        "min_severity": "warning",
        "priority": 0,
        "replayable": True,
        "when_hint": "Sign-in, step-up and session signals; never sampled.",
    },
    {
        "route": "access",
        "match": "access.",
        "label": "Cell-level access decisions",
        "min_severity": "info",
        "priority": 0,
        "replayable": True,
        "when_hint": "Row/column masking decisions; replayable because they are pure.",
    },
    {
        "route": "risk",
        "match": "risk.",
        "label": "Risk scoring",
        "min_severity": "info",
        "priority": 1,
        "replayable": True,
        "when_hint": "Score evaluations; high replay value for incident forensics.",
    },
    {
        "route": "billing",
        "match": "billing.",
        "label": "Payments, arrears and points",
        "min_severity": "info",
        "priority": 0,
        "replayable": True,
        "when_hint": "Money movement; highest priority and never sampled.",
    },
    {
        "route": "data",
        "match": "data.",
        "label": "Bulk data / export events",
        "min_severity": "info",
        "priority": 3,
        "replayable": False,
        "when_hint": "High-volume bulk work; the main sampling target.",
    },
    {
        "route": "default",
        "match": "*",
        "label": "Unclassified signals",
        "min_severity": "info",
        "priority": 2,
        "replayable": True,
        "when_hint": "Anything without a matching prefix; kept at info priority.",
    },
]

# (route, severity) -> keep-rate in [0, 1]. Longest route match wins.
SAMPLING_RULES: list[dict[str, Any]] = [
    {"route": "security", "severity": "critical", "keep_rate": 1.0, "when_hint": "Never sample a critical security event."},
    {"route": "access", "severity": "critical", "keep_rate": 1.0, "when_hint": "Access denials are the audit record."},
    {"route": "billing", "severity": "critical", "keep_rate": 1.0, "when_hint": "Money movement is never sampled."},
    {"route": "security", "severity": "info", "keep_rate": 0.25, "when_hint": "Routine successful sign-ins are 4:1 amplified."},
    {"route": "data", "severity": "info", "keep_rate": 0.05, "when_hint": "Bulk data events are heavily sampled down."},
    {"route": "default", "severity": "info", "keep_rate": 0.5, "when_hint": "Unclassified info is halved."},
]

# tenant id -> max events in flight. "*" is the per-tenant default.
ADMISSION_LIMITS: list[dict[str, Any]] = [
    {"tenant": "*", "max_in_flight": 0, "when_hint": "0 = only the global queue cap applies."},
]

REPLAY_POLICY: dict[str, Any] = {
    "max_attempts": REPLAY_ATTEMPTS,
    "backoff_seconds": REPLAY_BACKOFF_SECONDS,
    "backoff_multiplier": 2.0,
    "retryable_routes": sorted(
        {route["route"] for route in PIPELINE_ROUTES if route.get("replayable") and route["route"] != "default"}
    ),
    "note": "Replay re-submits retained dead letters; it never re-derives them.",
}


@dataclass(frozen=True)
class PipelineEvent:
    """One unit of work flowing through the pipeline."""

    event_id: str
    kind: str
    occurred_at: datetime
    severity: str
    entity_type: str
    entity_id: str
    actor_user_id: int | None
    tenant_id: str | None
    payload: dict = field(default_factory=dict)
    route: str = ""
    priority: int = 2
    attempts: int = 0
    first_failed_at: str = ""
    last_error: str = ""



Sink = Callable[[list[PipelineEvent]], Awaitable[None]]


def severity_rank(severity: str | None) -> int:
    """Queue priority for a severity (lower drains first). Unknown -> default."""
    return SEVERITY_PRIORITY.get(str(severity or DEFAULT_SEVERITY), SEVERITY_PRIORITY[DEFAULT_SEVERITY])


def resolve_route(kind: str | None) -> dict[str, Any]:
    """Longest-prefix match of an event kind against ``PIPELINE_ROUTES``.

    Returns a copy of the route row (never the module-level dict, so a caller
    cannot mutate the config table by accident).
    """
    text = str(kind or "").lower()
    best: dict[str, Any] | None = None
    best_len = -1
    for route in PIPELINE_ROUTES:
        needle = str(route.get("match", ""))
        if needle == "*":
            continue
        if text.startswith(needle) and len(needle) > best_len:
            best, best_len = route, len(needle)
    if best is None:
        best = PIPELINE_ROUTES[-1]
    return dict(best)


def keep_rate_for(route: str, severity: str | None) -> float:
    """Sampling keep-rate for (route, severity); ``1.0`` when no rule matches."""
    for rule in SAMPLING_RULES:
        if rule["route"] == route and rule["severity"] == str(severity or DEFAULT_SEVERITY):
            return float(rule["keep_rate"])
    return 1.0


def should_sample(
    kind: str,
    severity: str | None,
    *,
    route: str | None = None,
    counter: int = 0,
) -> bool:
    """Deterministic keep/drop decision for an event.

    Deterministic on purpose: a counter modulo the keep-rate means the same
    event index always lands the same way, so a sample is reproducible from the
    log instead of being a coin flip nobody can audit. Critical severities are
    exempt via the config table, not via a hardcoded branch.
    """
    resolved = route or resolve_route(kind)["route"]
    rate = keep_rate_for(resolved, severity)
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    period = max(1, int(round(1.0 / rate)))
    return (int(counter) % period) == 0


def admission_limit(tenant_id: str | None) -> int:
    """Max events one tenant may have in flight (0 = unbounded, use queue cap)."""
    for limit in ADMISSION_LIMITS:
        if limit["tenant"] == str(tenant_id or ""):
            return int(limit["max_in_flight"])
    return int(ADMISSION_LIMITS[0]["max_in_flight"])


def replayable(kind: str | None) -> bool:
    """May an event of this kind be re-submitted from the dead-letter ring?"""
    return bool(resolve_route(kind).get("replayable"))


def fanout_sink(sinks: list[Sink], *, names: list[str] | None = None) -> Sink:
    """Fan one batch out to several sinks, isolating each failure.

    A sink that raises is logged and counted; the remaining sinks still run and
    the failures surface as an aggregate at the end, so one broken destination
    cannot silently discard the batch for the others — nor can it stop the
    healthy destinations from receiving it.
    """
    targets = list(sinks)
    if not targets:
        raise ValueError("fanout_sink requires at least one sink")
    labels = list(names or [f"sink_{i}" for i in range(len(targets))])

    async def _fanout(batch: list[PipelineEvent]) -> None:
        failures: list[str] = []
        for label, sink in zip(labels, targets):
            try:
                await sink(batch)
            except Exception as exc:  # isolate one destination
                failures.append(f"{label}: {exc}")
                logger.error("Pipeline fanout sink %s failed: %s", label, exc)
        if failures:
            raise RuntimeError("; ".join(failures))

    return _fanout


def protobuf_transaction_sink(log: TransactionLog | None = None) -> Sink:
    """Default sink: encode each event as an immutable protobuf transaction."""

    async def _sink(batch: list[PipelineEvent]) -> None:
        log_ref = log or get_default_transaction_log()
        for event in batch:
            payload = dict(event.payload or {})
            payload.setdefault("severity", event.severity)
            log_ref.append(
                tx_id=event.event_id,
                action=event.kind,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                actor_user_id=event.actor_user_id,
                tenant_id=event.tenant_id,
                payload=payload,
                occurred_at=event.occurred_at,
            )

    return _sink


class HighThroughputPipeline:
    """Bounded queue + worker pool processing batches of ``PipelineEvent``."""

    def __init__(
        self,
        *,
        name: str = "security_signals",
        workers: int | None = None,
        batch_size: int | None = None,
        max_queue: int | None = None,
        flush_seconds: float | None = None,
        sink: Sink | None = None,
        sample: bool = False,
        enforce_admission: bool = False,
        priority_order: bool = False,
    ):
        self.name = name
        self.workers = workers if workers is not None else PIPELINE_WORKERS
        self.batch_size = batch_size if batch_size is not None else PIPELINE_BATCH_SIZE
        self.max_queue = max_queue if max_queue is not None else PIPELINE_MAX_QUEUE
        self.flush_seconds = flush_seconds if flush_seconds is not None else PIPELINE_FLUSH_SECONDS
        self._sink = sink or protobuf_transaction_sink()
        # All three delivery policies are opt-in: the default pipeline must
        # submit, order and retain events exactly as it did before they existed.
        self.sample = bool(sample)
        self.enforce_admission = bool(enforce_admission)
        self.priority_order = bool(priority_order)
        self._queue: asyncio.Queue[PipelineEvent] = asyncio.Queue(maxsize=self.max_queue)
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._dead_letter: deque[dict[str, Any]] = deque(maxlen=DEAD_LETTER_MAX)
        self._in_flight: dict[str, int] = {}
        self._sample_counter = 0
        self._by_route: dict[str, int] = {}
        self._by_severity: dict[str, int] = {}
        self._stats = {
            "submitted": 0,
            "processed": 0,
            "batches": 0,
            "rejected": 0,
            "dead_lettered": 0,
            "sampled_out": 0,
            "throttled": 0,
            "replayed": 0,
        }

    # --- submission -----------------------------------------------------------

    def submit(self, event: PipelineEvent) -> PipelineEvent | None:
        """Enqueue an event; returns the event, or ``None`` when the queue is full.

        ``None`` is the single "not accepted" signal for every rejection reason
        (full queue, tenant throttle, sampling) so the caller's 503 path is
        unchanged; the reason is always available in ``stats()``.
        """
        route = resolve_route(event.kind)
        if self.sample and not should_sample(
            event.kind,
            event.severity,
            route=route["route"],
            counter=self._sample_counter,
        ):
            self._sample_counter += 1
            self._stats["sampled_out"] += 1
            return None
        self._sample_counter += 1
        if self.enforce_admission and not self._admit(event.tenant_id):
            self._stats["throttled"] += 1
            logger.warning("Pipeline tenant throttle; event %s rejected", event.event_id)
            return None
        prepared = replace(
            event,
            route=route["route"],
            priority=severity_rank(event.severity),
        )
        try:
            self._queue.put_nowait(prepared)
        except asyncio.QueueFull:
            self._stats["rejected"] += 1
            logger.warning("Pipeline queue full; event %s rejected", event.event_id)
            return None
        self._in_flight[str(event.tenant_id or "")] = (
            self._in_flight.get(str(event.tenant_id or ""), 0) + 1
        )
        self._by_route[prepared.route] = self._by_route.get(prepared.route, 0) + 1
        self._by_severity[str(prepared.severity or DEFAULT_SEVERITY)] = (
            self._by_severity.get(str(prepared.severity or DEFAULT_SEVERITY), 0) + 1
        )
        self._stats["submitted"] += 1
        return prepared

    def _admit(self, tenant_id: str | None) -> bool:
        limit = admission_limit(tenant_id)
        if limit <= 0:
            return True
        return self._in_flight.get(str(tenant_id or ""), 0) < limit

    def submit_event(
        self,
        *,
        kind: str,
        severity: str = "info",
        entity_type: str = "system",
        entity_id: str = "",
        actor_user_id: int | None = None,
        tenant_id: str | None = None,
        payload: dict | None = None,
    ) -> PipelineEvent | None:
        """Build and enqueue an event in one call."""
        return self.submit(
            PipelineEvent(
                event_id=uuid.uuid4().hex,
                kind=kind,
                occurred_at=datetime.now(timezone.utc),
                severity=severity,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_user_id=actor_user_id,
                tenant_id=tenant_id,
                payload=dict(payload or {}),
            )
        )

    # --- replay ---------------------------------------------------------------

    def dead_letter_events(self) -> list[PipelineEvent]:
        """Rebuild the retained dead letters as replayable events.

        A dead letter that the policy says is not replayable is skipped rather
        than dropped: it stays in the inspection ring, it just never re-enters
        the queue.
        """
        return [
            replace(
                PipelineEvent(
                    event_id=str(entry.get("event_id", "")),
                    kind=str(entry.get("kind", "")),
                    occurred_at=_parse_moment(entry.get("occurred_at")) or datetime.now(timezone.utc),
                    severity=str(entry.get("severity", DEFAULT_SEVERITY)),
                    entity_type=str(entry.get("entity_type", "system")),
                    entity_id=str(entry.get("entity_id", "")),
                    actor_user_id=entry.get("actor_user_id"),
                    tenant_id=entry.get("tenant_id"),
                    payload=dict(entry.get("payload") or {}),
                ),
                attempts=int(entry.get("attempts", 1)),
                first_failed_at=str(entry.get("first_failed_at", "")),
                last_error=str(entry.get("error", "")),
            )
            for entry in reversed(self._dead_letter)
            if replayable(entry.get("kind"))
        ]

    async def replay_dead_letters(
        self,
        *,
        max_attempts: int | None = None,
        backoff_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Re-submit retained dead letters under ``REPLAY_POLICY``.

        Bounded by ``max_attempts`` (a letter that has already failed that many
        times is left in the ring for a human), exponentially backed off, and
        the ring is cleared of the letters that were actually re-queued so a
        later replay does not double-send.
        """
        budget = int(max_attempts if max_attempts is not None else REPLAY_POLICY["max_attempts"])
        base = float(
            backoff_seconds if backoff_seconds is not None else REPLAY_POLICY["backoff_seconds"]
        )
        multiplier = float(REPLAY_POLICY["backoff_multiplier"])
        requeued: list[str] = []
        exhausted: list[str] = []
        for event in self.dead_letter_events():
            if event.attempts >= budget:
                exhausted.append(event.event_id)
                continue
            if self.submit(event) is not None:
                requeued.append(event.event_id)
        self._dead_letter = deque(
            (entry for entry in self._dead_letter if entry.get("event_id") not in set(requeued)),
            maxlen=DEAD_LETTER_MAX,
        )
        self._stats["replayed"] += len(requeued)
        if requeued and base > 0:
            await asyncio.sleep(base * (multiplier ** max(0, len(requeued) - 1)))
        return {
            "attempted": len(requeued) + len(exhausted),
            "requeued": requeued,
            "exhausted": exhausted,
            "exhausted_count": len(exhausted),
            "max_attempts": budget,
            "backoff_seconds": base,
            "remaining": len(self._dead_letter),
        }


    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"pipeline-{self.name}-{index}")
            for index in range(self.workers)
        ]
        logger.info("Pipeline '%s' started with %d workers", self.name, self.workers)

    async def stop(self) -> None:
        """Cancel workers and flush whatever remains (best-effort drain)."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self.drain()

    def _ordered(self, batch: list[PipelineEvent]) -> list[PipelineEvent]:
        """Sort a drained batch by severity priority when ``priority_order`` is on.

        **Keep-as-is decision:** the default is *append order*. The transaction
        log is append-only and hash-chained, and ``tail()``/``verify_chain()``
        (plus the pinned expectation that the first submitted event is the first
        one written) depend on it. Reordering is therefore an explicit opt-in,
        not a silent improvement.
        """
        if not self.priority_order:
            return batch
        return sorted(batch, key=lambda e: (e.priority, e.occurred_at))

    async def drain(self) -> None:
        """Synchronously flush everything currently queued (no workers needed)."""
        batch: list[PipelineEvent] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._flush(self._ordered(batch))

    # --- workers --------------------------------------------------------------

    async def _worker(self, worker_id: int) -> None:
        loop = asyncio.get_running_loop()
        while self._running:
            batch: list[PipelineEvent] = []
            deadline = loop.time() + self.flush_seconds
            while len(batch) < self.batch_size:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=max(0.0, remaining))
                except asyncio.TimeoutError:
                    break
                batch.append(item)
                self._queue.task_done()
            if batch:
                await self._flush(self._ordered(batch))

    def _release(self, batch: list[PipelineEvent]) -> None:
        """Decrement per-tenant in-flight counters for a flushed batch."""
        for event in batch:
            key = str(event.tenant_id or "")
            remaining = self._in_flight.get(key, 0) - 1
            if remaining > 0:
                self._in_flight[key] = remaining
            else:
                self._in_flight.pop(key, None)

    async def _flush(self, batch: list[PipelineEvent]) -> None:
        try:
            await self._sink(batch)
            self._stats["processed"] += len(batch)
        except Exception as exc:  # never lose events silently
            self._stats["processed"] += len(batch)
            self._stats["dead_lettered"] += len(batch)
            logger.error("Pipeline batch flush failed (dead-lettering %d): %s", len(batch), exc)
            now = datetime.now(timezone.utc).isoformat()
            for event in batch:
                previous = next(
                    (e for e in self._dead_letter if e.get("event_id") == event.event_id),
                    None,
                )
                self._dead_letter.append(
                    {
                        "event_id": event.event_id,
                        "kind": event.kind,
                        "occurred_at": event.occurred_at.isoformat(),
                        "error": str(exc),
                        # replay bookkeeping: carried so a retry can bound itself
                        "severity": event.severity,
                        "entity_type": event.entity_type,
                        "entity_id": event.entity_id,
                        "actor_user_id": event.actor_user_id,
                        "tenant_id": event.tenant_id,
                        "payload": dict(event.payload or {}),
                        "route": event.route or resolve_route(event.kind)["route"],
                        "replayable": replayable(event.kind),
                        "attempts": int((previous or {}).get("attempts", 0)) + 1,
                        "first_failed_at": (previous or {}).get("first_failed_at") or now,
                        "last_failed_at": now,
                    }
                )
        self._release(batch)
        self._stats["batches"] += 1

    # --- introspection --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": self._running,
            "workers": self.workers,
            "batch_size": self.batch_size,
            "max_queue": self.max_queue,
            "flush_seconds": self.flush_seconds,
            "pending": self._queue.qsize(),
            "submitted": self._stats["submitted"],
            "processed": self._stats["processed"],
            "batches": self._stats["batches"],
            "rejected": self._stats["rejected"],
            "dead_lettered": self._stats["dead_lettered"],
            "sampled_out": self._stats["sampled_out"],
            "throttled": self._stats["throttled"],
            "replayed": self._stats["replayed"],
            "in_flight_by_tenant": {k: v for k, v in sorted(self._in_flight.items()) if v > 0},
            "by_route": dict(sorted(self._by_route.items())),
            "by_severity": dict(sorted(self._by_severity.items())),
            "policies": {
                "sample": self.sample,
                "enforce_admission": self.enforce_admission,
                "priority_order": self.priority_order,
            },
        }

    def dead_letter_report(self) -> dict[str, Any]:
        """Why the ring is what it is, without touching it.

        The counters say how many events failed; the breakdown says *which kind*
        keeps failing, which is the difference between "the sink was down for a
        moment" (one kind, recent) and "this kind is structurally unserialisable"
        (one kind, permanent). ``retained`` can be lower than ``dead_lettered``
        because the ring is bounded, and ``replayable`` can be lower than
        ``retained`` because :func:`replayable` excludes kinds the replay policy
        refuses to re-send.
        """
        ring = list(self._dead_letter)
        by_kind: dict[str, int] = {}
        by_error: dict[str, int] = {}
        attempts = 0
        for entry in ring:
            kind = str(entry.get("kind", ""))
            by_kind[kind] = by_kind.get(kind, 0) + 1
            error = str(entry.get("error", ""))
            by_error[error] = by_error.get(error, 0) + 1
            attempts = max(attempts, int(entry.get("attempts", 0) or 0))
        occurred = sorted(str(entry.get("occurred_at", "")) for entry in ring if entry.get("occurred_at"))
        replayable_count = len(self.dead_letter_events())
        return {
            "dead_lettered": self._stats["dead_lettered"],
            "retained": len(ring),
            "replayable": replayable_count,
            "policy": dict(REPLAY_POLICY),
            "recent": ring[-20:],
            # --- additive breakdown -------------------------------------------
            "generated_at": datetime.now(timezone.utc),
            "ring_capacity": DEAD_LETTER_MAX,
            "evicted": max(0, self._stats["dead_lettered"] - len(ring)),
            "replayable_ratio": round(replayable_count / len(ring), 4) if ring else None,
            "blocked_by_policy": len(ring) - replayable_count,
            "max_attempts_seen": attempts,
            "by_kind": dict(sorted(by_kind.items(), key=lambda item: (-item[1], item[0]))),
            "by_error": dict(sorted(by_error.items(), key=lambda item: (-item[1], item[0]))),
            "oldest_occurred_at": occurred[0] if occurred else None,
            "newest_occurred_at": occurred[-1] if occurred else None,
            "drainable": replayable_count > 0,
            "note": (
                "inspection only; POST /audit/pipeline/replay is what drains the ring"
            ),
        }


_default_pipeline: HighThroughputPipeline | None = None


def get_default_pipeline() -> HighThroughputPipeline:
    global _default_pipeline
    if _default_pipeline is None:
        _default_pipeline = HighThroughputPipeline(sink=protobuf_transaction_sink())
    return _default_pipeline


def set_default_pipeline(pipeline: HighThroughputPipeline | None) -> None:
    global _default_pipeline
    _default_pipeline = pipeline


def build_pipeline_catalog() -> dict[str, object]:
    """Introspectable delivery contract for ``/meta`` tooling.

    ``tunables`` keeps its exact historical four-key set (pinned by contract);
    the delivery policy added by the thin-group expansion is nested under its
    own keys so it can never collide with the throughput knobs.
    """
    return {
        "tunables": {
            "workers": PIPELINE_WORKERS,
            "batch_size": PIPELINE_BATCH_SIZE,
            "max_queue": PIPELINE_MAX_QUEUE,
            "flush_seconds": PIPELINE_FLUSH_SECONDS,
        },
        "autostart": PIPELINE_AUTOSTART,
        "backpressure": "bounded queue; full queue rejects with 503 (submit returns None)",
        "failure_policy": f"dead-letter (bounded ring of {DEAD_LETTER_MAX})",
        "default_sink": "protobuf_transaction_sink (immutable hash-chained frames)",
        "current": {
            "pending": get_default_pipeline().stats()["pending"],
            "processed": get_default_pipeline().stats()["processed"],
        },
        "severity_priority": dict(SEVERITY_PRIORITY),
        "routes": [dict(route) for route in PIPELINE_ROUTES],
        "sampling": {
            "rules": [dict(rule) for rule in SAMPLING_RULES],
            "default_keep_rate": 1.0,
            "deterministic": "keep when (counter mod round(1/keep_rate)) == 0",
            "opt_in": "pipeline.sample=False by default; every event is kept",
        },
        "admission": {
            "limits": [dict(limit) for limit in ADMISSION_LIMITS],
            "opt_in": "pipeline.enforce_admission=False by default",
        },
        "ordering": {
            "default": "append order (submission order is a pinned contract)",
            "priority_order": "opt-in via pipeline.priority_order=True",
            "helper": "severity_rank(severity)",
        },
        "replay": {
            **dict(REPLAY_POLICY),
            "helper": "replay_dead_letters(); re-submits retained letters only",
            "inspect": "dead_letter_report(); never mutates the ring",
        },
        "fanout": {
            "helper": "fanout_sink(sinks, names=...)",
            "isolation": "one failing sink is logged; the rest still receive the batch",
        },
        "helpers": {
            "resolve_route": "resolve_route(kind) -> longest-prefix route row",
            "should_sample": "should_sample(kind, severity, counter=n)",
            "keep_rate_for": "keep_rate_for(route, severity)",
            "admission_limit": "admission_limit(tenant_id)",
            "replayable": "replayable(kind)",
            "severity_rank": "severity_rank(severity)",
        },
    }


def _parse_moment(value: Any) -> datetime | None:
    """Parse an ISO-8601 string (tolerating a trailing ``Z``) to an aware datetime."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed