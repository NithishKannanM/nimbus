"""The job state machine is the control plane's contract. Every legal move is
asserted, and -- more importantly -- every illegal move is asserted to raise."""

from __future__ import annotations

import itertools

import pytest

from backend.models.enums import TERMINAL_STATUSES, JobEvent, JobStatus
from backend.models.job import IllegalTransition, Job, TRANSITIONS, next_status
from backend.models.workload_helpers import make_job  # noqa: F401  (see below)


def test_every_status_event_pair_is_decided():
    """No (status, event) pair may be undefined: it either transitions or raises."""
    for status, event in itertools.product(JobStatus, JobEvent):
        if (status, event) in TRANSITIONS:
            assert isinstance(next_status(status, event), JobStatus)
        else:
            with pytest.raises(IllegalTransition):
                next_status(status, event)


def test_terminal_states_have_no_outgoing_transitions():
    for status in TERMINAL_STATUSES:
        outgoing = [e for (s, e) in TRANSITIONS if s == status]
        assert outgoing == [], f"{status} should be terminal, found {outgoing}"


def test_happy_path_sets_timestamps():
    job = make_job()
    assert job.status is JobStatus.QUEUED

    job = job.apply(JobEvent.SCHEDULE)
    assert job.status is JobStatus.SCHEDULED and job.scheduled_at is not None

    job = job.apply(JobEvent.ACCEPTED)
    assert job.status is JobStatus.RUNNING and job.started_at is not None

    job = job.apply(JobEvent.COMPLETED)
    assert job.status is JobStatus.COMPLETED
    assert job.completed_at is not None
    assert job.execution_time_ms is not None and job.execution_time_ms >= 0


def test_retry_path_clears_assignment():
    job = make_job().apply(JobEvent.SCHEDULE).model_copy(update={"assigned_worker": "worker-1"})
    job = job.apply(JobEvent.ACCEPTED).apply(JobEvent.WORKER_LOST)
    assert job.status is JobStatus.RETRYING

    job = job.apply(JobEvent.REQUEUE)
    assert job.status is JobStatus.QUEUED
    assert job.assigned_worker is None
    assert job.started_at is None and job.scheduled_at is None


def test_exhausted_path_is_terminal():
    job = make_job().apply(JobEvent.SCHEDULE).apply(JobEvent.ACCEPTED)
    job = job.apply(JobEvent.TIMEOUT)
    assert job.status is JobStatus.TIMED_OUT
    job = job.apply(JobEvent.EXHAUSTED)
    assert job.status is JobStatus.FAILED
    with pytest.raises(IllegalTransition):
        job.apply(JobEvent.REQUEUE)


def test_cannot_complete_a_job_that_never_started():
    with pytest.raises(IllegalTransition):
        make_job().apply(JobEvent.COMPLETED)


def test_dispatch_rejection_returns_to_queue():
    job = make_job().apply(JobEvent.SCHEDULE).model_copy(update={"assigned_worker": "worker-2"})
    job = job.apply(JobEvent.DISPATCH_REJECTED)
    assert job.status is JobStatus.QUEUED
    assert job.assigned_worker is None


def test_execution_id_is_unique_per_attempt():
    job = make_job()
    first = job.model_copy(update={"attempts": 1}).execution_id
    second = job.model_copy(update={"attempts": 2}).execution_id
    assert first != second
    assert first.startswith(job.job_id)


def test_apply_does_not_mutate_the_original():
    job = make_job()
    job.apply(JobEvent.SCHEDULE)
    assert job.status is JobStatus.QUEUED


# --------------------------------------------------------------------------- #
# dead-lettering (Phase 4)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status",
    [
        JobStatus.QUEUED,
        JobStatus.SCHEDULED,
        JobStatus.RUNNING,
        JobStatus.RETRYING,
        JobStatus.TIMED_OUT,
    ],
)
def test_dead_lettering_is_terminal_from_every_live_state(status: JobStatus):
    """Including QUEUED, which no other failure event can leave.

    A dead-lettered message is one the queue has stopped delivering. Its job
    cannot be retried no matter how much budget it has left, and it cannot be
    left live either -- a live job with no deliverable message is a job that
    can never resolve.
    """
    assert next_status(status, JobEvent.DEAD_LETTER) is JobStatus.FAILED


def test_dead_lettering_does_not_consult_the_retry_budget():
    job = make_job().model_copy(update={"attempts": 0, "max_attempts": 10})
    dead = job.apply(JobEvent.DEAD_LETTER)
    assert dead.status is JobStatus.FAILED
    assert dead.completed_at is not None
    assert dead.attempts == 0, "dead-lettering is not an attempt"


def test_a_terminal_job_cannot_be_dead_lettered():
    for status in TERMINAL_STATUSES:
        with pytest.raises(IllegalTransition):
            next_status(status, JobEvent.DEAD_LETTER)
