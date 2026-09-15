"""In-process queue with real SQS-like visibility timeouts and a DLQ.

Real timers, real receipt handles, real redrive. If a concurrency bug can
happen against SQS it should be reproducible here.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from backend.services.queue.base import Lease, LeaseExpired, QueueDepth


@dataclass
class _Message:
    job_id: str
    visible_at: float
    receive_count: int = 0
    receipt: str | None = None
    enqueued_at: float = field(default_factory=time.monotonic)


class LocalJobQueue:
    def __init__(
        self,
        *,
        visibility_timeout_s: float = 60.0,
        max_receive_count: int = 3,
        clock=time.monotonic,
    ) -> None:
        self._visibility_timeout_s = visibility_timeout_s
        self._max_receive_count = max_receive_count
        self._clock = clock
        self._lock = threading.RLock()
        self._not_empty = threading.Condition(self._lock)
        self._messages: dict[str, _Message] = {}
        self._order: list[str] = []
        self._dlq: list[str] = []
        #: Redriven but not yet reconciled with the job record. Drained by
        #: sweep_expired(). Two different paths move a message to the DLQ --
        #: receive() on its way past, and the reaper's explicit sweep -- and
        #: the control plane has to hear about it either way, or a job whose
        #: message was redriven during a receive would sit QUEUED forever with
        #: nothing left to deliver it.
        self._dlq_pending: list[str] = []

    # -- producer ---------------------------------------------------------
    def enqueue(self, job_id: str, *, delay_s: float = 0.0) -> None:
        with self._not_empty:
            if job_id in self._messages:
                # Re-enqueueing a job already in the queue is a control-plane
                # bug, not something to paper over.
                raise ValueError(f"{job_id} is already queued")
            self._messages[job_id] = _Message(job_id, self._clock() + delay_s)
            self._order.append(job_id)
            self._not_empty.notify_all()

    # -- consumer ---------------------------------------------------------
    def receive(self, max_messages: int = 1, wait_s: float = 0.0) -> list[Lease]:
        deadline = self._clock() + wait_s
        with self._not_empty:
            while True:
                leases = self._receive_locked(max_messages)
                if leases or wait_s <= 0:
                    return leases
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return []
                self._not_empty.wait(timeout=min(remaining, 0.05))

    def _receive_locked(self, max_messages: int) -> list[Lease]:
        now = self._clock()
        leases: list[Lease] = []
        for job_id in list(self._order):
            if len(leases) >= max_messages:
                break
            msg = self._messages.get(job_id)
            if msg is None or msg.visible_at > now:
                continue
            if msg.receive_count >= self._max_receive_count:
                self._to_dlq_locked(msg)
                continue
            msg.receive_count += 1
            msg.receipt = uuid.uuid4().hex
            msg.visible_at = now + self._visibility_timeout_s
            leases.append(Lease(msg.job_id, msg.receipt, msg.receive_count))
        return leases

    def _to_dlq_locked(self, msg: _Message) -> None:
        self._messages.pop(msg.job_id, None)
        self._order.remove(msg.job_id)
        self._dlq.append(msg.job_id)
        self._dlq_pending.append(msg.job_id)

    # -- lease management --------------------------------------------------
    def _held(self, lease: Lease) -> _Message:
        msg = self._messages.get(lease.job_id)
        if msg is None or msg.receipt != lease.receipt:
            raise LeaseExpired(f"stale receipt for {lease.job_id}")
        return msg

    def extend(self, lease: Lease, visibility_s: float) -> None:
        with self._lock:
            self._held(lease).visible_at = self._clock() + visibility_s

    def ack(self, lease: Lease) -> None:
        with self._lock:
            self._held(lease)
            self._messages.pop(lease.job_id)
            self._order.remove(lease.job_id)

    def nack(self, lease: Lease) -> None:
        with self._not_empty:
            msg = self._held(lease)
            msg.visible_at = self._clock()
            msg.receipt = None
            self._not_empty.notify_all()

    def release(self, lease: Lease, delay_s: float = 0.0) -> None:
        """Return a *failed* attempt to the queue after a backoff delay.

        The difference from :meth:`defer` is the delivery count, and it is the
        whole point of having both. Deferral means "the fleet had no room right
        now"; the job did not fail, no attempt was consumed, and walking it
        toward the DLQ would dead-letter healthy work. Release means "this
        attempt failed"; the delivery count *must* stand, because that count is
        the poison-message backstop and a job that fails every time it runs is
        exactly what the DLQ is for.

        ``nack`` is the zero-delay special case. It exists separately because a
        nack is also how an immediate re-placement is requested, and reading
        ``release(lease, 0)`` at a call site would not say that.

        SQS equivalent: ChangeMessageVisibility to ``delay_s``. Visibility is
        the only lever there too -- ApproximateReceiveCount cannot be moved in
        either direction, which is why deferral had to be modelled as a
        delete-and-resend and this must not be.
        """
        with self._not_empty:
            msg = self._held(lease)
            msg.visible_at = self._clock() + delay_s
            msg.receipt = None
            self._not_empty.notify_all()

    def defer(self, lease: Lease, delay_s: float = 0.0) -> None:
        """Put a job back because the control plane chose not to place it *now*.

        Atomic, and deliberately so. The previous implementation was
        ``ack()`` followed by ``enqueue(delay_s=...)``, which reset the delivery
        count correctly but left a window where the message existed in neither
        state -- a drain check landing in that gap saw an empty queue and an
        idle fleet, and declared the run finished while a job was still
        pending. That truncated a 60-job benchmark to 59.

        Deferral is not a delivery, so the receive count is rolled back: a job
        that no worker had room for has not failed and must not be walked toward
        the dead-letter queue.

        The SQS implementation (Phase 5) will use ChangeMessageVisibility, which
        is likewise atomic; it cannot roll back ApproximateReceiveCount, which is
        why the DLQ threshold sits above the retry budget.
        """
        with self._not_empty:
            msg = self._held(lease)
            msg.visible_at = self._clock() + delay_s
            msg.receipt = None
            msg.receive_count = max(0, msg.receive_count - 1)
            self._not_empty.notify_all()

    # -- introspection -----------------------------------------------------
    def depth(self) -> QueueDepth:
        with self._lock:
            now = self._clock()
            visible = sum(1 for m in self._messages.values() if m.visible_at <= now)
            return QueueDepth(visible=visible, in_flight=len(self._messages) - visible)

    def dead_letters(self) -> list[str]:
        with self._lock:
            return list(self._dlq)

    def sweep_expired(self) -> list[str]:
        """Move poison messages to the DLQ without handing them out.

        ``receive()`` already redrives on the way past, but only for messages
        it actually reaches: a message that is over the limit and never
        received again -- because the fleet is saturated, or because the
        scheduler stopped -- would sit visible forever. The reaper calls this
        so redrive does not depend on anyone trying to consume the message one
        more time.

        Returns the job ids moved, so the caller can mark their jobs FAILED.
        A dead-lettered message with a live job record is a job that can never
        resolve, which is worse than either failure on its own.
        """
        with self._lock:
            now = self._clock()
            for job_id in list(self._order):
                msg = self._messages[job_id]
                if msg.visible_at <= now and msg.receive_count >= self._max_receive_count:
                    self._to_dlq_locked(msg)
            # Includes anything receive() redrove since the last sweep, not
            # just what this call moved. The caller's contract is "job ids that
            # need their records terminated", and which code path did the
            # redrive is not the caller's problem.
            pending, self._dlq_pending = self._dlq_pending, []
            return pending
