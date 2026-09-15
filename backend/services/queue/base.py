"""Queue abstraction, modelled on SQS semantics.

The interface is deliberately SQS-shaped -- lease, extend, ack, nack,
receive_count, DLQ -- so that behaviour observed against ``LocalJobQueue``
transfers to ``SQSJobQueue`` in Phase 5 (docs/architecture.md section 3.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Lease:
    job_id: str
    receipt: str
    receive_count: int


@dataclass(frozen=True)
class QueueDepth:
    visible: int
    in_flight: int

    @property
    def total(self) -> int:
        return self.visible + self.in_flight


class LeaseExpired(Exception):
    """The receipt handle is stale: the lease expired and the message was
    handed to someone else (or redelivered)."""


@runtime_checkable
class JobQueue(Protocol):
    def enqueue(self, job_id: str, *, delay_s: float = 0.0) -> None: ...

    def receive(self, max_messages: int = 1, wait_s: float = 0.0) -> list[Lease]: ...

    def extend(self, lease: Lease, visibility_s: float) -> None: ...

    def ack(self, lease: Lease) -> None: ...

    def nack(self, lease: Lease) -> None: ...

    #: A failed attempt, returned after backoff. Keeps the delivery count.
    def release(self, lease: Lease, delay_s: float = 0.0) -> None: ...

    #: Not a failure: the fleet had no room. Rolls the delivery count back.
    def defer(self, lease: Lease, delay_s: float = 0.0) -> None: ...

    def depth(self) -> QueueDepth: ...

    def dead_letters(self) -> list[str]: ...

    #: Redrive poison messages without waiting for another receive attempt.
    def sweep_expired(self) -> list[str]: ...
