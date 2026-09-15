"""LocalJobQueue must reproduce SQS semantics: lease, visibility timeout,
receipt invalidation, redrive to DLQ. If it doesn't, behaviour verified locally
won't transfer to Phase 5."""

from __future__ import annotations

import pytest

from backend.services.queue.base import LeaseExpired
from backend.services.queue.local import LocalJobQueue


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def queue(clock: FakeClock) -> LocalJobQueue:
    return LocalJobQueue(visibility_timeout_s=30.0, max_receive_count=3, clock=clock)


def test_fifo_receive_order(queue: LocalJobQueue):
    for i in range(3):
        queue.enqueue(f"job-{i}")
    leases = queue.receive(max_messages=3)
    assert [lease.job_id for lease in leases] == ["job-0", "job-1", "job-2"]


def test_received_message_is_invisible(queue: LocalJobQueue):
    queue.enqueue("job-a")
    assert queue.receive(1)
    assert queue.receive(1) == []
    assert queue.depth().in_flight == 1
    assert queue.depth().visible == 0


def test_visibility_timeout_redelivers(queue: LocalJobQueue, clock: FakeClock):
    queue.enqueue("job-a")
    first = queue.receive(1)[0]
    clock.advance(31.0)
    second = queue.receive(1)[0]
    assert second.job_id == "job-a"
    assert second.receive_count == 2
    assert second.receipt != first.receipt


def test_stale_receipt_is_rejected(queue: LocalJobQueue, clock: FakeClock):
    queue.enqueue("job-a")
    stale = queue.receive(1)[0]
    clock.advance(31.0)
    queue.receive(1)
    with pytest.raises(LeaseExpired):
        queue.ack(stale)


def test_ack_removes_the_message(queue: LocalJobQueue):
    queue.enqueue("job-a")
    queue.ack(queue.receive(1)[0])
    assert queue.depth().total == 0


def test_nack_makes_it_immediately_visible(queue: LocalJobQueue):
    queue.enqueue("job-a")
    queue.nack(queue.receive(1)[0])
    assert queue.depth().visible == 1
    assert queue.receive(1)[0].receive_count == 2


def test_extend_defers_redelivery(queue: LocalJobQueue, clock: FakeClock):
    queue.enqueue("job-a")
    lease = queue.receive(1)[0]
    clock.advance(25.0)
    queue.extend(lease, 30.0)
    clock.advance(10.0)
    assert queue.receive(1) == []
    clock.advance(25.0)
    assert queue.receive(1) != []


def test_delayed_enqueue_is_not_immediately_visible(queue: LocalJobQueue, clock: FakeClock):
    queue.enqueue("job-a", delay_s=5.0)
    assert queue.receive(1) == []
    clock.advance(5.0)
    assert queue.receive(1) != []


def test_redrive_to_dlq_after_max_receive_count(queue: LocalJobQueue, clock: FakeClock):
    queue.enqueue("job-poison")
    for expected in (1, 2, 3):
        lease = queue.receive(1)[0]
        assert lease.receive_count == expected
        clock.advance(31.0)
    assert queue.receive(1) == []
    assert queue.dead_letters() == ["job-poison"]
    assert queue.depth().total == 0


def test_duplicate_enqueue_is_a_bug_not_a_silent_noop(queue: LocalJobQueue):
    queue.enqueue("job-a")
    with pytest.raises(ValueError):
        queue.enqueue("job-a")


def test_receive_respects_max_messages(queue: LocalJobQueue):
    for i in range(5):
        queue.enqueue(f"job-{i}")
    assert len(queue.receive(max_messages=2)) == 2
    assert queue.depth().visible == 3


def test_defer_keeps_the_message_visible_to_the_queue_at_all_times(queue: LocalJobQueue):
    """Regression: deferral used to be ack-then-enqueue, leaving a window where
    the job existed in neither the queue nor a worker. A drain check landing in
    that gap declared a still-running benchmark finished, truncating a 60-job
    run to 59."""
    queue.enqueue("job-a")
    lease = queue.receive(1)[0]
    assert queue.depth().total == 1

    queue.defer(lease, 0.0)
    assert queue.depth().total == 1, "the job must never vanish from the queue"
    assert queue.depth().visible == 1


def test_defer_does_not_consume_the_retry_budget(queue: LocalJobQueue, clock: FakeClock):
    """A job nobody had room for has not failed, so repeatedly deferring it must
    never dead-letter it."""
    queue.enqueue("job-a")
    for _ in range(10):
        lease = queue.receive(1)[0]
        assert lease.receive_count == 1
        queue.defer(lease, 0.0)

    assert queue.dead_letters() == []
    assert queue.depth().total == 1


def test_nack_still_consumes_the_retry_budget(queue: LocalJobQueue):
    """Deferral and genuine failure must stay distinguishable."""
    queue.enqueue("job-a")
    queue.nack(queue.receive(1)[0])
    assert queue.receive(1)[0].receive_count == 2


def test_defer_with_delay_hides_the_message_until_it_elapses(
    queue: LocalJobQueue, clock: FakeClock
):
    queue.enqueue("job-a")
    queue.defer(queue.receive(1)[0], 5.0)
    assert queue.receive(1) == []
    assert queue.depth().in_flight == 1
    clock.advance(5.0)
    assert queue.receive(1) != []


def test_deferring_a_stale_lease_is_rejected(queue: LocalJobQueue, clock: FakeClock):
    queue.enqueue("job-a")
    stale = queue.receive(1)[0]
    clock.advance(31.0)
    queue.receive(1)
    with pytest.raises(LeaseExpired):
        queue.defer(stale, 0.0)


# --------------------------------------------------------------------------- #
# release: a failed attempt, returned after backoff (Phase 4)
# --------------------------------------------------------------------------- #
def test_release_keeps_the_delivery_count(queue: LocalJobQueue):
    """The difference between release and defer, and the reason both exist.

    Release means "this attempt failed". The delivery count has to stand,
    because that count is the poison-message backstop: a job that fails every
    time it runs is exactly what the DLQ is for.
    """
    queue.enqueue("job-a")
    queue.release(queue.receive(1)[0], 0.0)
    assert queue.receive(1)[0].receive_count == 2


def test_defer_rolls_the_delivery_count_back(queue: LocalJobQueue):
    """Deferral means "the fleet had no room". Nothing failed, so walking the
    job toward the DLQ would dead-letter healthy work."""
    queue.enqueue("job-a")
    queue.defer(queue.receive(1)[0], 0.0)
    assert queue.receive(1)[0].receive_count == 1


def test_release_honours_its_backoff_delay(queue: LocalJobQueue, clock: FakeClock):
    queue.enqueue("job-a")
    queue.release(queue.receive(1)[0], 10.0)
    assert queue.receive(1) == []
    clock.advance(9.0)
    assert queue.receive(1) == []
    clock.advance(2.0)
    assert queue.receive(1) != []


def test_releasing_a_stale_lease_is_rejected(queue: LocalJobQueue, clock: FakeClock):
    queue.enqueue("job-a")
    stale = queue.receive(1)[0]
    clock.advance(31.0)
    queue.receive(1)
    with pytest.raises(LeaseExpired):
        queue.release(stale, 0.0)


def test_repeated_releases_walk_a_poison_message_into_the_dlq(queue: LocalJobQueue):
    """max_receive_count=3, so the fourth delivery attempt redrives instead."""
    queue.enqueue("job-poison")
    for _ in range(3):
        queue.release(queue.receive(1)[0], 0.0)
    assert queue.receive(1) == []
    assert queue.dead_letters() == ["job-poison"]


# --------------------------------------------------------------------------- #
# redrive notification
# --------------------------------------------------------------------------- #
def test_sweep_reports_messages_redriven_by_receive(queue: LocalJobQueue):
    """Two paths move a message to the DLQ -- receive() on its way past, and
    the reaper's explicit sweep. The control plane has to hear about it either
    way, or a job whose message was redriven during a receive would sit QUEUED
    forever with nothing left to deliver it.
    """
    queue.enqueue("job-poison")
    for _ in range(3):
        queue.nack(queue.receive(1)[0])
    assert queue.receive(1) == []          # redriven here, by receive()
    assert queue.dead_letters() == ["job-poison"]

    assert queue.sweep_expired() == ["job-poison"], "the redrive was never reported"


def test_a_redrive_is_reported_only_once(queue: LocalJobQueue):
    queue.enqueue("job-poison")
    for _ in range(3):
        queue.nack(queue.receive(1)[0])
    queue.receive(1)
    assert queue.sweep_expired() == ["job-poison"]
    assert queue.sweep_expired() == []


def test_sweep_redrives_a_message_nobody_tried_to_receive(queue: LocalJobQueue):
    """receive() only redrives messages it actually reaches. A message over the
    limit that is never received again -- saturated fleet, stopped scheduler --
    would otherwise sit visible forever."""
    queue.enqueue("job-poison")
    for _ in range(3):
        queue.nack(queue.receive(1)[0])

    assert queue.dead_letters() == []      # nothing has tried to receive it yet
    assert queue.sweep_expired() == ["job-poison"]
    assert queue.dead_letters() == ["job-poison"]


def test_sweep_leaves_healthy_messages_alone(queue: LocalJobQueue):
    queue.enqueue("job-a")
    queue.enqueue("job-b")
    assert queue.sweep_expired() == []
    assert queue.depth().visible == 2
