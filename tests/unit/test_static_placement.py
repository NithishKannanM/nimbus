"""Static Resource-Aware placement.

Covers feasibility filtering, the frozen scoring formula, determinism, the
effect of reservations -- and, most importantly, telemetry isolation: the
static policy must reach the same answer no matter what the host is observed to
be doing.
"""

from __future__ import annotations

import pytest

from backend.models.workload_helpers import make_job
from scheduler import build_scheduler
from scheduler.placement.static_resource_aware import (
    STATIC_WEIGHTS,
    StaticResourceAwarePlacement,
)
from scheduler.views import job_requirements, static_fleet_view, static_view
from tests.conftest import make_fleet, telemetry, worker_state


def place(fleet, job=None, strategy="static_resource_aware"):
    return build_scheduler(strategy).place(job or make_job(), fleet)


def select(policy, job, *states):
    return policy.select_worker(
        job_requirements(job), static_fleet_view(tuple(states)), None
    )


# --------------------------------------------------------------------------- #
# feasibility
# --------------------------------------------------------------------------- #
def test_worker_without_enough_declared_cpu_is_rejected():
    job = make_job(cpu_units=1024, memory_mb=128)
    outcome = select(
        StaticResourceAwarePlacement(),
        job,
        worker_state("worker-1", cpu_capacity=2048, cpu_reserved=1536),
        worker_state("worker-2", cpu_capacity=2048, cpu_reserved=0),
    )
    assert outcome.selected_worker == "worker-2"
    assert "insufficient cpu units" in outcome.excluded["worker-1"]


def test_worker_without_enough_declared_memory_is_rejected():
    job = make_job(cpu_units=128, memory_mb=2048)
    outcome = select(
        StaticResourceAwarePlacement(),
        job,
        worker_state("worker-1", memory_capacity=4096, memory_reserved=3072),
        worker_state("worker-2", memory_capacity=4096, memory_reserved=0),
    )
    assert outcome.selected_worker == "worker-2"
    assert "insufficient memory" in outcome.excluded["worker-1"]


def test_worker_with_no_free_slot_is_rejected():
    outcome = select(
        StaticResourceAwarePlacement(),
        make_job(cpu_units=128, memory_mb=128),
        worker_state("worker-1", slots=2, active_jobs=2),
        worker_state("worker-2", slots=2, active_jobs=1),
    )
    assert outcome.selected_worker == "worker-2"
    assert "no free slot" in outcome.excluded["worker-1"]


def test_unhealthy_worker_is_rejected_even_when_it_scores_best():
    """An idle but unhealthy worker would win on score. Health is a hard
    constraint, not a term."""
    from backend.models.enums import WorkerStatus

    outcome = select(
        StaticResourceAwarePlacement(),
        make_job(cpu_units=256, memory_mb=256),
        worker_state("worker-1", status=WorkerStatus.UNHEALTHY),  # completely idle
        worker_state("worker-2", cpu_reserved=1024, memory_reserved=2048, active_jobs=1),
    )
    assert outcome.selected_worker == "worker-2"
    assert outcome.excluded["worker-1"] == "worker not healthy"


def test_no_feasible_worker_places_nothing():
    outcome = select(
        StaticResourceAwarePlacement(),
        make_job(cpu_units=1024, memory_mb=128),
        worker_state("worker-1", cpu_reserved=2048),
        worker_state("worker-2", slots=1, active_jobs=1),
    )
    assert outcome.selected_worker is None
    assert len(outcome.excluded) == 2


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def test_weights_are_frozen_and_normalised():
    assert STATIC_WEIGHTS == {"cpu": 0.40, "memory": 0.30, "slots": 0.30}
    assert sum(STATIC_WEIGHTS.values()) == pytest.approx(1.0)


def test_score_matches_the_documented_formula():
    job = make_job(cpu_units=512, memory_mb=1024)
    view = static_view(
        worker_state("w", slots=2, cpu_capacity=2048, cpu_reserved=512,
                     memory_capacity=4096, memory_reserved=1024, active_jobs=1)
    )
    terms = StaticResourceAwarePlacement.score_terms(job_requirements(job), view)
    expected = (
        0.40 * ((512 + 512) / 2048)
        + 0.30 * ((1024 + 1024) / 4096)
        + 0.30 * ((1 + 1) / 2)
    )
    assert StaticResourceAwarePlacement.score(terms) == pytest.approx(expected)


def test_score_is_bounded_in_zero_one():
    job = job_requirements(make_job(cpu_units=1024, memory_mb=2048))
    for reserved in (0, 512, 1024):
        view = static_view(worker_state("w", cpu_reserved=reserved, memory_reserved=reserved))
        score = StaticResourceAwarePlacement.score(
            StaticResourceAwarePlacement.score_terms(job, view)
        )
        assert 0.0 <= score <= 1.0


def test_balance_mode_prefers_the_least_reserved_worker():
    outcome = select(
        StaticResourceAwarePlacement(mode="balance"),
        make_job(cpu_units=256, memory_mb=256),
        worker_state("worker-1", cpu_reserved=1536, memory_reserved=3072, active_jobs=1),
        worker_state("worker-2", cpu_reserved=0, memory_reserved=0, active_jobs=0),
        worker_state("worker-3", cpu_reserved=512, memory_reserved=1024, active_jobs=1),
    )
    assert outcome.selected_worker == "worker-2"
    assert outcome.scores["worker-2"] < outcome.scores["worker-3"] < outcome.scores["worker-1"]


def test_binpack_mode_prefers_the_most_reserved_feasible_worker():
    """The ECS analogue: opposite objective, same information."""
    outcome = select(
        StaticResourceAwarePlacement(mode="binpack"),
        make_job(cpu_units=256, memory_mb=256),
        worker_state("worker-1", cpu_reserved=1536, memory_reserved=3072, active_jobs=1),
        worker_state("worker-2", cpu_reserved=0, memory_reserved=0, active_jobs=0),
        worker_state("worker-3", cpu_reserved=512, memory_reserved=1024, active_jobs=1),
    )
    assert outcome.selected_worker == "worker-1"


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        StaticResourceAwarePlacement(mode="telemetry")  # type: ignore[arg-type]


def test_policy_name_identifies_the_mode():
    """Regression: both modes shared one implementation, so a trace recording
    only the class name replayed a binpack decision as a balance decision."""
    assert StaticResourceAwarePlacement(mode="balance").name == "static_resource_aware"
    assert (
        StaticResourceAwarePlacement(mode="binpack").name == "static_resource_aware_binpack"
    )


def test_contributions_sum_to_the_score():
    outcome = select(
        StaticResourceAwarePlacement(),
        make_job(cpu_units=512, memory_mb=512),
        worker_state("worker-1", cpu_reserved=256, memory_reserved=512, active_jobs=1),
    )
    terms = outcome.terms["worker-1"]
    total = terms["contrib_cpu"] + terms["contrib_memory"] + terms["contrib_slots"]
    assert total == pytest.approx(outcome.scores["worker-1"])


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_identical_workers_break_ties_deterministically():
    fleet = make_fleet(
        worker_state("worker-1"), worker_state("worker-2"), worker_state("worker-3")
    )
    picks = {place(fleet).selected_worker for _ in range(20)}
    assert picks == {"worker-1"}, "identical workers must always yield the same choice"


def test_tie_break_ignores_input_order():
    a, b, c = worker_state("worker-1"), worker_state("worker-2"), worker_state("worker-3")
    for ordering in ((a, b, c), (c, b, a), (b, c, a)):
        assert place(make_fleet(*ordering)).selected_worker == "worker-1"


def test_binpack_ties_also_break_on_worker_id():
    outcome = select(
        StaticResourceAwarePlacement(mode="binpack"),
        make_job(cpu_units=256, memory_mb=256),
        worker_state("worker-3"), worker_state("worker-1"), worker_state("worker-2"),
    )
    assert outcome.selected_worker == "worker-1"


# --------------------------------------------------------------------------- #
# reservations feed back into subsequent scores
# --------------------------------------------------------------------------- #
def test_reservation_changes_the_next_decision():
    """Placing onto a worker must make it less attractive next time; otherwise
    every job in a batch piles onto the same worker."""
    policy = StaticResourceAwarePlacement()
    job = make_job(cpu_units=512, memory_mb=512)

    before = select(policy, job, worker_state("worker-1"), worker_state("worker-2"))
    assert before.selected_worker == "worker-1"

    # Simulate the engine's reservation of worker-1 after that placement.
    after = select(
        policy, job,
        worker_state("worker-1", cpu_reserved=512, memory_reserved=512, active_jobs=1),
        worker_state("worker-2"),
    )
    assert after.selected_worker == "worker-2"
    assert after.scores["worker-1"] > before.scores["worker-1"]


def test_repeated_placement_spreads_across_the_fleet():
    """Balance mode with feedback should rotate rather than stack."""
    policy = StaticResourceAwarePlacement()
    reserved = {"worker-1": 0, "worker-2": 0, "worker-3": 0}
    picks = []
    for _ in range(6):
        states = [
            worker_state(
                w, cpu_reserved=reserved[w] * 512, memory_reserved=reserved[w] * 512,
                active_jobs=reserved[w],
            )
            for w in sorted(reserved)
        ]
        outcome = select(policy, make_job(cpu_units=512, memory_mb=512), *states)
        picks.append(outcome.selected_worker)
        reserved[outcome.selected_worker] += 1
    assert sorted(reserved.values()) == [2, 2, 2], f"uneven spread: {reserved}"


# --------------------------------------------------------------------------- #
# telemetry isolation -- the critical regression test
# --------------------------------------------------------------------------- #
def test_static_placement_ignores_observed_cpu_pressure():
    """The scenario from the Phase 2 brief.

    worker-1 wins on static score but is observed at 99% CPU; worker-2 loses on
    static score but is observed nearly idle. Static Resource-Aware MUST still
    choose worker-1.

    This looks like a weakness, and it is the entire point: it is the control
    that lets Phase 3 measure whether observed telemetry adds anything.
    """
    fleet = make_fleet(
        worker_state(
            "worker-1", cpu_reserved=0, memory_reserved=0, active_jobs=0,
            observed=telemetry("worker-1", cpu=99.0, memory=95.0),
        ),
        worker_state(
            "worker-2", cpu_reserved=1024, memory_reserved=2048, active_jobs=1,
            observed=telemetry("worker-2", cpu=5.0, memory=10.0),
        ),
    )
    decision = place(fleet, make_job(cpu_units=256, memory_mb=256))

    assert decision.selected_worker == "worker-1"
    assert decision.candidates["worker-1"].score < decision.candidates["worker-2"].score
    # ...and the telemetry it ignored is nonetheless in the trace.
    assert decision.observed_cpu("worker-1") == 99.0
    assert decision.observed_cpu("worker-2") == 5.0


def test_placement_is_invariant_to_telemetry_content():
    """Same static state, wildly different observations -> identical decision,
    including identical scores."""
    def build(cpu1: float, cpu2: float):
        return make_fleet(
            worker_state("worker-1", cpu_reserved=256, active_jobs=1,
                         observed=telemetry("worker-1", cpu=cpu1, memory=cpu1)),
            worker_state("worker-2", cpu_reserved=512, active_jobs=1,
                         observed=telemetry("worker-2", cpu=cpu2, memory=cpu2)),
        )

    idle = place(build(0.0, 0.0), make_job(cpu_units=256, memory_mb=256))
    inverted = place(build(100.0, 0.0), make_job(cpu_units=256, memory_mb=256))

    assert idle.selected_worker == inverted.selected_worker == "worker-1"
    assert idle.candidates["worker-1"].score == inverted.candidates["worker-1"].score
    assert idle.candidates["worker-2"].score == inverted.candidates["worker-2"].score


def test_placement_is_invariant_to_telemetry_being_absent():
    """Trace content changes; the decision does not."""
    def build(with_telemetry: bool):
        return make_fleet(
            worker_state("worker-1", cpu_reserved=256, active_jobs=1,
                         observed=telemetry("worker-1", cpu=97.0) if with_telemetry else None),
            worker_state("worker-2", cpu_reserved=512, active_jobs=1,
                         observed=telemetry("worker-2", cpu=2.0) if with_telemetry else None),
        )

    with_t = place(build(True), make_job(cpu_units=256, memory_mb=256))
    without_t = place(build(False), make_job(cpu_units=256, memory_mb=256))

    assert with_t.selected_worker == without_t.selected_worker
    assert with_t.candidates["worker-1"].score == without_t.candidates["worker-1"].score
    assert with_t.candidates["worker-1"].observed is not None
    assert without_t.candidates["worker-1"].observed is None


def test_static_policy_receives_only_static_views():
    """Structural check: whatever the policy is handed has no telemetry."""
    policy = StaticResourceAwarePlacement()
    states = (worker_state("worker-1", observed=telemetry("worker-1", cpu=88.0)),)
    views = policy.build_views(states)
    assert all(type(v).__name__ == "StaticWorkerView" for v in views)
    assert not any(hasattr(v, "observed") or hasattr(v, "cpu_percent") for v in views)


def test_binpack_margin_is_non_negative():
    """Regression: margin was computed assuming a minimising objective, so
    binpack reported a negative margin against the candidate *furthest* from its
    own choice. Runner-up must mean "who would have won instead"."""
    fleet = make_fleet(
        worker_state("worker-1", cpu_reserved=1536, memory_reserved=3072, active_jobs=1),
        worker_state("worker-2", cpu_reserved=0, memory_reserved=0),
        worker_state("worker-3", cpu_reserved=512, memory_reserved=1024, active_jobs=1),
    )
    job = make_job(cpu_units=256, memory_mb=256)

    packed = place(fleet, job, strategy="static_resource_aware_binpack")
    assert packed.selected_worker == "worker-1"
    assert packed.runner_up == "worker-3", "next-fullest, not emptiest"
    assert packed.margin is not None and packed.margin > 0

    balanced = place(fleet, job, strategy="static_resource_aware")
    assert balanced.selected_worker == "worker-2"
    assert balanced.runner_up == "worker-3"
    assert balanced.margin is not None and balanced.margin > 0
