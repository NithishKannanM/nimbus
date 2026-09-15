"""SQS-backed job queue.

``LocalJobQueue`` was written to SQS's semantics rather than the other way
round, so this adapter is mostly a translation table. That was the point of
building it that way: everything the local queue taught us -- that deliveries
are not attempts, that deferral has to reset the delivery count and a retry must
not, that redrive can happen on a path nobody is watching -- transfers here
without being relearned against a paid, rate-limited, eventually-consistent API.

Where the mapping is *not* one-to-one, it is called out in the method that
differs, because those are the places where a local result would mislead.

| Nimbus | SQS |
|---|---|
| ``enqueue(delay_s)`` | ``SendMessage`` with ``DelaySeconds`` |
| ``receive`` | ``ReceiveMessage`` with long polling |
| ``extend`` | ``ChangeMessageVisibility`` |
| ``ack`` | ``DeleteMessage`` |
| ``nack`` | ``ChangeMessageVisibility(0)`` |
| ``release(delay)`` | ``ChangeMessageVisibility(delay)`` |
| ``defer(delay)`` | ``DeleteMessage`` + ``SendMessage(DelaySeconds)`` |
| ``dead_letters`` | receive from the DLQ |
| ``sweep_expired`` | drain newly-arrived DLQ messages |
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from backend.services.queue.base import Lease, LeaseExpired, QueueDepth

log = logging.getLogger("nimbus.queue.sqs")

#: SQS caps DelaySeconds at 15 minutes. Our backoff cap is 30 s by default, so
#: this only matters if someone configures a pathological retry_cap_s -- in
#: which case silently clamping is better than a rejected send that loses a job.
MAX_DELAY_S = 900

#: SQS caps a single ReceiveMessage at 10.
MAX_RECEIVE_BATCH = 10

#: Long-poll duration. Nonzero on purpose: short polling bills a request per
#: empty poll and samples only a subset of hosts, so an idle scheduler ticking
#: every 20 ms would generate 50 billable empty receives a second and still miss
#: messages that are there.
DEFAULT_WAIT_S = 1


class SQSJobQueue:
    """The Phase 5 queue. Same protocol, same semantics, different substrate."""

    def __init__(
        self,
        queue_url: str,
        *,
        dlq_url: str | None = None,
        visibility_timeout_s: float = 60.0,
        region: str | None = None,
        client: Any = None,
    ) -> None:
        if not queue_url:
            raise ValueError("queue_url is required for the SQS backend")
        self._url = queue_url
        self._dlq_url = dlq_url
        self._visibility_timeout_s = visibility_timeout_s
        if client is None:
            from backend.aws.clients import client as make_client  # noqa: PLC0415

            client = make_client("sqs", region=region)
        self._sqs = client
        self._lock = threading.Lock()
        #: Job ids seen in the DLQ, so sweep_expired reports each one once.
        #: SQS has no "what changed" API -- the DLQ is just another queue, and
        #: reading it does not remove anything -- so the de-duplication has to
        #: live on this side.
        self._reported_dead: set[str] = set()

    # -- producer ---------------------------------------------------------
    def enqueue(self, job_id: str, *, delay_s: float = 0.0) -> None:
        self._sqs.send_message(
            QueueUrl=self._url,
            MessageBody=job_id,
            DelaySeconds=int(min(max(delay_s, 0.0), MAX_DELAY_S)),
        )

    # -- consumer ---------------------------------------------------------
    def receive(self, max_messages: int = 1, wait_s: float = 0.0) -> list[Lease]:
        """Lease up to ``max_messages`` jobs.

        Unlike the local queue this can return fewer messages than exist, and
        can return none when the queue is non-empty: ReceiveMessage samples a
        subset of the hosts holding the queue. That is a property of SQS, not a
        bug to work around -- the scheduler loop simply tries again on its next
        tick, which is also what it does locally when the fleet is full.
        """
        response = self._sqs.receive_message(
            QueueUrl=self._url,
            MaxNumberOfMessages=min(max(int(max_messages), 1), MAX_RECEIVE_BATCH),
            WaitTimeSeconds=int(wait_s) if wait_s else DEFAULT_WAIT_S,
            VisibilityTimeout=int(self._visibility_timeout_s),
            AttributeNames=["ApproximateReceiveCount"],
        )
        leases: list[Lease] = []
        for message in response.get("Messages", []):
            attributes = message.get("Attributes", {})
            leases.append(
                Lease(
                    job_id=message["Body"],
                    receipt=message["ReceiptHandle"],
                    receive_count=int(attributes.get("ApproximateReceiveCount", 1)),
                )
            )
        return leases

    # -- lease management --------------------------------------------------
    def extend(self, lease: Lease, visibility_s: float) -> None:
        self._change_visibility(lease, visibility_s)

    def ack(self, lease: Lease) -> None:
        try:
            self._sqs.delete_message(QueueUrl=self._url, ReceiptHandle=lease.receipt)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, lease) from exc

    def nack(self, lease: Lease) -> None:
        self._change_visibility(lease, 0.0)

    def release(self, lease: Lease, delay_s: float = 0.0) -> None:
        """A failed attempt, back after backoff, delivery count intact.

        The count is exactly what we want kept: it is the redrive policy's
        input, and a job that fails every time it runs is what the DLQ exists
        for.
        """
        self._change_visibility(lease, delay_s)

    def defer(self, lease: Lease, delay_s: float = 0.0) -> None:
        """Not a failure -- the fleet had no room -- so the delivery count must
        not stand.

        ``ApproximateReceiveCount`` cannot be decremented; there is no API for
        it. The only way to reset it is to delete the message and send a new
        one, which is what this does, and which is why it is the one operation
        in this class that is **not atomic**.

        That non-atomicity is a real difference from the local queue, where
        ``defer`` is a single locked operation precisely because the delete-then-
        send version cost Phase 3 three silently-truncated benchmark runs. Here
        the gap cannot be closed, only bounded: the send is issued immediately
        after the delete, and a crash in between loses the job. The reaper is
        the backstop -- the job is still live in DynamoDB with no lease, which
        is exactly the orphan case it sweeps for.
        """
        self.ack(lease)
        self.enqueue(lease.job_id, delay_s=delay_s)

    def _change_visibility(self, lease: Lease, seconds: float) -> None:
        try:
            self._sqs.change_message_visibility(
                QueueUrl=self._url,
                ReceiptHandle=lease.receipt,
                VisibilityTimeout=int(min(max(seconds, 0.0), 43200)),  # 12 h SQS cap
            )
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, lease) from exc

    @staticmethod
    def _translate(exc: Exception, lease: Lease) -> Exception:
        """Map SQS's receipt errors onto ``LeaseExpired``.

        The engine's whole retry path is written against ``LeaseExpired``.
        Letting a ``ClientError`` through would take an ordinary,expected
        condition -- a lease that lapsed while a job ran long -- and turn it
        into an unhandled exception in the scheduler loop.
        """
        name = type(exc).__name__
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("ReceiptHandleIsInvalid", "InvalidParameterValue") or "Receipt" in name:
            return LeaseExpired(f"stale receipt for {lease.job_id}: {code or name}")
        return exc

    # -- introspection -----------------------------------------------------
    def depth(self) -> QueueDepth:
        """Approximate by construction -- the attribute names say so.

        Good enough for a dashboard and for the scheduler's "is there work"
        check. Not good enough for a drain check, which is why
        ``NimbusEngine.wait_for_idle`` consults job state in the store as well
        (the Phase 3 defect that truncated a 60-job run to 59).
        """
        attributes = self._sqs.get_queue_attributes(
            QueueUrl=self._url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            ],
        )["Attributes"]
        visible = int(attributes.get("ApproximateNumberOfMessages", 0))
        delayed = int(attributes.get("ApproximateNumberOfMessagesDelayed", 0))
        in_flight = int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0))
        # A delayed message is invisible but owned by nobody. Counting it as
        # in-flight keeps `total` honest, which is what the drain check reads.
        return QueueDepth(visible=visible, in_flight=in_flight + delayed)

    def dead_letters(self) -> list[str]:
        """Every job id currently in the DLQ.

        Reading a queue does not remove anything from it, so this peeks with a
        zero visibility timeout: the messages stay put and stay visible for the
        next reader. It is a report, not a consumer.
        """
        return sorted(self._peek_dlq())

    def sweep_expired(self) -> list[str]:
        """Job ids that have newly appeared in the DLQ since the last sweep.

        There is nothing to sweep on this side: SQS's redrive policy moves
        messages past ``maxReceiveCount`` on its own. What the reaper actually
        needs from this call is the *notification* -- which jobs got redriven --
        so their records can be terminated. A dead-lettered message whose job is
        still live in DynamoDB is a job that can never resolve.
        """
        with self._lock:
            found = self._peek_dlq()
            fresh = sorted(found - self._reported_dead)
            self._reported_dead |= found
        if fresh:
            log.warning("dead-lettered: %s", fresh)
        return fresh

    def _peek_dlq(self) -> set[str]:
        if not self._dlq_url:
            return set()
        seen: set[str] = set()
        # One page is enough per sweep: the reaper runs on a timer, so anything
        # missed here is picked up on the next pass rather than lost.
        response = self._sqs.receive_message(
            QueueUrl=self._dlq_url,
            MaxNumberOfMessages=MAX_RECEIVE_BATCH,
            WaitTimeSeconds=0,
            VisibilityTimeout=0,
        )
        for message in response.get("Messages", []):
            seen.add(message["Body"])
        return seen

    def purge(self) -> None:
        """Empty the queue. Test and teardown only -- SQS rate-limits PurgeQueue
        to one call per minute per queue."""
        self._sqs.purge_queue(QueueUrl=self._url)
        with self._lock:
            self._reported_dead.clear()
