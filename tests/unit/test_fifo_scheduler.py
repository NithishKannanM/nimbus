"""FIFO ordering and round-robin placement -- the Phase 1 baseline, preserved.

Phase 2 split ``Scheduler`` into ``OrderingPolicy`` + ``PlacementPolicy``. Every
Phase 1 assertion is carried over here against the new shapes; none was dropped.

Policies are pure functions of their inputs, so these need no engine, no threads
and no clock.
"""

from __future__ import annotations

from datetime import timedelta

from backend.models.enums import WorkerStatus
from backend.models.job import utcnow
from backend.models.workload_helpers import make_job
from scheduler import build_scheduler
from scheduler.base import infeasibility_reason
from scheduler.ordering import FIFOOrdering, effective_priority
from scheduler.views import job_requirements, static_view
from tests.conftest import make_fleet, worker_state

THREE_IDLE = make_fleet(
    worker_state("worker-1"), worker_state("worker-2"), worker_state("worker-3")
)


def static(worker, job=None):
    return static_view(worker)


# -- ordering ------------------------------------------------------------
def test_fifo_selects_earliest_arrival():
    now = utcnow()
    old = make_job().model_copy(update={"created_at": now - timedelta(seconds=10)})
    new = make_job().model_copy(update={"created_at": now})
    assert FIFOOrdering().select_job([new, old], now) is old


def test_fifo_ignores_priority():
    now = utcnow()
    low = make_job(priority=4).model_copy(update={"created_at": now - timedelta(seconds=1)})
    critical = make_job(priority=1).model_copy(update={"created_at": now})
    assert FIFOOrdering().select_job([critical, low], now) is low


def test_select_next_on_empty_queue_is_none():
    assert build_scheduler("fifo").select_next([], utcnow()) is None


# -- placement -----------------------------------------------------------
def test_round_robin_cycles_through_workers():
    strategy = build_scheduler("fifo")
    picks = [strategy.place(make_job(), THREE_IDLE).selected_worker for _ in range(6)]
    assert picks == ["worker-1", "worker-2", "worker-3"] * 2


def test_round_robin_skips_ineligible_workers():
    fleet = make_fleet(
        worker_state("worker-1"),
        worker_state("worker-2", active_jobs=2),
        worker_state("worker-3"),
    )
    strategy = build_scheduler("fifo")
    picks = [strategy.place(make_job(), fleet).selected_worker for _ in range(4)]
    assert set(picks) == {"worker-1", "worker-3"}
    assert "worker-2" not in picks


def test_decision_records_every_candidate_and_exclusion():
    fleet = make_fleet(
        worker_state("worker-1"),
        worker_state("worker-2"),
        worker_state("worker-3", status=WorkerStatus.UNHEALTHY, heartbeat_age_s=31.4),
    )
    decision = build_scheduler("fifo").place(make_job(), fleet)

    # Every worker appears in the trace; eligibility is a flag, not an omission.
    assert set(decision.candidates) == {"worker-1", "worker-2", "worker-3"}
    assert decision.eligible_workers == ["worker-1", "worker-2"]
    assert decision.candidates["worker-3"].eligible is False
    assert "worker-3" in decision.excluded
    assert "not healthy" in decision.excluded["worker-3"]
    assert decision.reason
    assert decision.decision_latency_us > 0


def test_no_eligible_worker_places_nothing():
    full = make_fleet(
        worker_state("worker-1", active_jobs=2), worker_state("worker-2", active_jobs=2)
    )
    decision = build_scheduler("fifo").place(make_job(), full)
    assert decision.selected_worker is None
    assert len(decision.excluded) == 2
    assert "no eligible worker" in decision.reason


def test_runner_up_and_margin_are_recorded():
    decision = build_scheduler("fifo").place(make_job(), THREE_IDLE)
    assert decision.runner_up is not None
    assert decision.margin is not None


# -- feasibility filter --------------------------------------------------
def test_unhealthy_workers_are_never_candidates():
    job = job_requirements(make_job())
    for status in (WorkerStatus.UNHEALTHY, WorkerStatus.DRAINING, WorkerStatus.DEAD):
        reason = infeasibility_reason(job, static(worker_state("w", status=status)))
        assert reason == "worker not healthy"


def test_full_worker_is_excluded():
    job = job_requirements(make_job())
    assert "no free slot" in infeasibility_reason(job, static(worker_state("w", active_jobs=2)))


def test_insufficient_cpu_units_excluded():
    job = job_requirements(make_job().model_copy(update={"cpu_units": 1024}))
    view = static(worker_state("w", cpu_reserved=1536))
    assert "insufficient cpu units" in infeasibility_reason(job, view)


def test_insufficient_memory_excluded():
    job = job_requirements(make_job().model_copy(update={"memory_mb": 2048}))
    view = static(worker_state("w", memory_reserved=3072))
    assert "insufficient memory" in infeasibility_reason(job, view)


def test_healthy_worker_with_room_is_eligible():
    assert infeasibility_reason(job_requirements(make_job()), static(worker_state("w"))) is None


# -- aging ---------------------------------------------------------------
def test_aging_promotes_a_low_priority_job():
    now = utcnow()
    job = make_job(priority=4).model_copy(update={"created_at": now - timedelta(seconds=65)})
    assert effective_priority(job, now, 30.0) == 2


def test_aging_never_goes_below_critical():
    now = utcnow()
    job = make_job(priority=4).model_copy(update={"created_at": now - timedelta(seconds=6000)})
    assert effective_priority(job, now, 30.0) == 1


def test_aging_disabled_returns_declared_priority():
    now = utcnow()
    job = make_job(priority=4).model_copy(update={"created_at": now - timedelta(seconds=600)})
    assert effective_priority(job, now, 0.0) == 4
