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
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from dataclasses import dataclass, field
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


Sink = Callable[[list[PipelineEvent]], Awaitable[None]]


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
    ):
        self.name = name
        self.workers = workers if workers is not None else PIPELINE_WORKERS
        self.batch_size = batch_size if batch_size is not None else PIPELINE_BATCH_SIZE
        self.max_queue = max_queue if max_queue is not None else PIPELINE_MAX_QUEUE
        self.flush_seconds = flush_seconds if flush_seconds is not None else PIPELINE_FLUSH_SECONDS
        self._sink = sink or protobuf_transaction_sink()
        self._queue: asyncio.Queue[PipelineEvent] = asyncio.Queue(maxsize=self.max_queue)
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._dead_letter: deque[dict[str, Any]] = deque(maxlen=DEAD_LETTER_MAX)
        self._stats = {
            "submitted": 0,
            "processed": 0,
            "batches": 0,
            "rejected": 0,
            "dead_lettered": 0,
        }

    # --- submission -----------------------------------------------------------

    def submit(self, event: PipelineEvent) -> PipelineEvent | None:
        """Enqueue an event; returns the event, or ``None`` when the queue is full."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._stats["rejected"] += 1
            logger.warning("Pipeline queue full; event %s rejected", event.event_id)
            return None
        self._stats["submitted"] += 1
        return event

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

    async def drain(self) -> None:
        """Synchronously flush everything currently queued (no workers needed)."""
        batch: list[PipelineEvent] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._flush(batch)

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
                await self._flush(batch)

    async def _flush(self, batch: list[PipelineEvent]) -> None:
        try:
            await self._sink(batch)
            self._stats["processed"] += len(batch)
        except Exception as exc:  # never lose events silently
            self._stats["processed"] += len(batch)
            self._stats["dead_lettered"] += len(batch)
            logger.error("Pipeline batch flush failed (dead-lettering %d): %s", len(batch), exc)
            for event in batch:
                self._dead_letter.append(
                    {
                        "event_id": event.event_id,
                        "kind": event.kind,
                        "occurred_at": event.occurred_at.isoformat(),
                        "error": str(exc),
                    }
                )
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
        }

    def dead_letter_report(self) -> dict[str, Any]:
        return {
            "dead_lettered": self._stats["dead_lettered"],
            "retained": len(self._dead_letter),
            "recent": list(self._dead_letter)[-20:],
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
    }