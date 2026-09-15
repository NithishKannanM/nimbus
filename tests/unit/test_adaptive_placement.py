"""adaptive-v1 — telemetry-aware placement.

Everything asserted here follows docs/adaptive-v1-preregistration.md, which was
written before this module existed. If a test and the pre-registration
disagree, the pre-registration wins and the algorithm is wrong.
"""

from __future__ import annotations

import pytest

from backend.models.enums import WorkerStatus, WorkloadClass
from backend.models.workload_helpers import make_job
from scheduler import build_scheduler
from scheduler.placement.adaptive import (
    ADAPTIVE_WEIGHTS,
    ALGORITHM_VERSION,
    COMPONENTS,
    TREND_SCALE,
    AdaptivePlacement,
)
from scheduler.views import adaptive_view, job_requirements
from tests.conftest import make_fleet, observed_worker, worker_state

MAX_AGE = 6.0


def place(fleet, job=None, strategy="adaptive"):
    return build_scheduler(strategy, telemetry_max_age_s=MAX_AGE).place(
        job or make_job(), fleet
    )


def select(job, *states, max_age_s: float = MAX_AGE):
    policy = AdaptivePlacement(telemetry_max_age_s=max_age_s)
    return policy.select_worker(
        job_requirements(job), policy.build_views(tuple(states)), None
    )


def components(job, state, *, max_age_s: float = MAX_AGE):
    return AdaptivePlacement.components(
        job_requirements(job), adaptive_view(state, max_age_s=max_age_s)
    )


# --------------------------------------------------------------------------- #
# pre-registered parameters
# --------------------------------------------------------------------------- #
def test_algorithm_version_is_pinned():
    assert ALGORITHM_VERSION == "adaptive-v1"
    assert AdaptivePlacement.algorithm_version == "adaptive-v1"


def test_weight_vectors_match_the_preregistration():
    assert ADAPTIVE_WEIGHTS == {
        WorkloadClass.CPU_BOUND: {
            "cpu": 0.50, "memory": 0.10, "reservation": 0.20, "queue": 0.15, "trend": 0.05},
        WorkloadClass.MEMORY_BOUND: {
            "cpu": 0.10, "memory": 0.50, "reservation": 0.20, "queue": 0.15, "trend": 0.05},
        WorkloadClass.IO_BOUND: {
            "cpu": 0.15, "memory": 0.15, "reservation": 0.25, "queue": 0.40, "trend": 0.05},
        WorkloadClass.LIGHTWEIGHT: {
            "cpu": 0.10, "memory": 0.05, "reservation": 0.25, "queue": 0.55, "trend": 0.05},
    }


def test_every_class_has_weights_summing_to_one():
    assert set(ADAPTIVE_WEIGHTS) == set(WorkloadClass)
    for workload_class, weights in ADAPTIVE_WEIGHTS.items():
        assert set(weights) == set(COMPONENTS), workload_class
        assert sum(weights.values()) == pytest.approx(1.0), workload_class


def test_score_is_bounded_in_zero_one():
    for cpu in (0.0, 50.0, 100.0):
        for reserved in (0, 1024, 2048):
            state = observed_worker("w", cpu=cpu, memory=cpu, cpu_reserved=reserved)
            job = make_job(cpu_units=0, memory_mb=0)
            score = AdaptivePlacement.score(
                components(job, state), AdaptivePlacement.weights_for(job.workload_class)
            )
            assert 0.0 <= score <= 1.0


# --------------------------------------------------------------------------- #
# fresh telemetry is consumed
# --------------------------------------------------------------------------- #
def test_fresh_observed_cpu_drives_the_cpu_component():
    state = observed_worker("w", cpu=73.0, age_s=0.5)
    assert components(make_job(), state)["cpu"] == pytest.approx(0.73)


def test_smoothed_value_is_used_not_the_latest_sample():
    """The pre-registered signal is the window mean, not the instantaneous
    reading -- a single spike must not swing a placement."""
    state = observed_worker("w", cpu=20.0)
    spiky = state.model_copy(
        update={
            "observed_window": state.observed_window.model_copy(
                update={"cpu_percent_mean": 20.0, "cpu_percent_latest": 99.0}
            )
        }
    )
    assert components(make_job(), spiky)["cpu"] == pytest.approx(0.20)


def test_memory_component_takes_the_worse_of_host_and_worker_pressure():
    host_heavy = observed_worker("w", memory=80.0, process_memory_mb=100.0,
                                 memory_capacity=4096)
    own_heavy = observed_worker("w", memory=10.0, process_memory_mb=3072.0,
                                memory_capacity=4096)
    assert components(make_job(), host_heavy)["memory"] == pytest.approx(0.80)
    assert components(make_job(), own_heavy)["memory"] == pytest.approx(0.75)


def test_rising_trend_scores_worse_than_falling():
    rising = components(make_job(), observed_worker("w", cpu=50.0, trend=+10.0))["trend"]
    flat = components(make_job(), observed_worker("w", cpu=50.0, trend=0.0))["trend"]
    falling = components(make_job(), observed_worker("w", cpu=50.0, trend=-10.0))["trend"]
    assert falling < flat < rising
    assert flat == pytest.approx(0.5)
    assert rising == pytest.approx(0.5 + 10.0 / (2 * TREND_SCALE))


def test_trend_saturates_rather_than_exploding():
    extreme = components(make_job(), observed_worker("w", cpu=50.0, trend=10_000.0))
    assert extreme["trend"] == 1.0


def test_reservation_component_is_the_static_model():
    state = observed_worker("w", cpu=5.0, cpu_reserved=512, memory_reserved=1024,
                            cpu_capacity=2048, memory_capacity=4096)
    job = make_job(cpu_units=512, memory_mb=1024)
    expected = 0.5 * ((512 + 512) / 2048) + 0.5 * ((1024 + 1024) / 4096)
    assert components(job, state)["reservation"] == pytest.approx(expected)


def test_adaptive_still_uses_static_information():
    """Not observed-only: two workers with identical telemetry must still be
    separated by their reservations."""
    outcome = select(
        make_job(cpu_units=512, memory_mb=256),
        observed_worker("worker-1", cpu=30.0, cpu_reserved=1536, memory_reserved=3072,
                        active_jobs=1),
        observed_worker("worker-2", cpu=30.0, cpu_reserved=0, memory_reserved=0),
    )
    assert outcome.selected_worker == "worker-2"


# --------------------------------------------------------------------------- #
# telemetry freshness
# --------------------------------------------------------------------------- #
def test_stale_telemetry_falls_back_to_reservation_estimates():
    stale = observed_worker("w", cpu=5.0, age_s=30.0, cpu_reserved=1024,
                            memory_reserved=2048)
    job = make_job(cpu_units=512, memory_mb=512)
    result = components(job, stale)

    view = adaptive_view(stale, max_age_s=MAX_AGE)
    assert view.telemetry_fresh is False
    assert result["cpu"] == pytest.approx(view.projected_cpu_utilisation(512))
    assert result["memory"] == pytest.approx(view.projected_memory_utilisation(512))
    assert result["trend"] == 0.5
    # The attractive 5% reading was NOT used.
    assert result["cpu"] != pytest.approx(0.05)


def test_missing_telemetry_is_not_read_as_idle():
    """The critical safety property: an unobserved worker must not become the
    most attractive candidate by scoring 0% CPU."""
    job = make_job(cpu_units=1024, memory_mb=2048)
    never_sampled = worker_state("worker-1", cpu_reserved=1024, memory_reserved=2048,
                                 active_jobs=1)
    result = components(job, never_sampled)

    assert adaptive_view(never_sampled, max_age_s=MAX_AGE).telemetry_fresh is False
    assert result["cpu"] > 0.0, "missing telemetry must never mean 0% CPU"
    assert result["cpu"] == pytest.approx((1024 + 1024) / 2048)


def test_a_worker_with_no_samples_is_stale_even_if_recent():
    state = observed_worker("w", cpu=10.0, samples=0, age_s=0.1)
    assert adaptive_view(state, max_age_s=MAX_AGE).telemetry_fresh is False


def test_freshness_threshold_is_respected_at_the_boundary():
    assert adaptive_view(observed_worker("w", age_s=MAX_AGE), max_age_s=MAX_AGE).telemetry_fresh
    assert not adaptive_view(
        observed_worker("w", age_s=MAX_AGE + 0.01), max_age_s=MAX_AGE
    ).telemetry_fresh


def test_unobserved_worker_does_not_beat_an_observed_idle_one():
    outcome = select(
        make_job(cpu_units=512, memory_mb=512),
        worker_state("worker-1", cpu_reserved=1024, memory_reserved=2048, active_jobs=1),
        observed_worker("worker-2", cpu=8.0, cpu_reserved=1024, memory_reserved=2048,
                        active_jobs=1),
    )
    assert outcome.selected_worker == "worker-2"


def test_all_stale_reduces_to_a_purely_static_decision():
    """With no fresh telemetry anywhere, adaptive-v1 consumes no observed state.

    It does not become bit-identical to static_resource_aware -- the effective
    weights differ -- but the *inputs* are DECLARED + RESERVED only, which is
    the property that matters.
    """
    outcome = select(
        make_job(cpu_units=512, memory_mb=512),
        observed_worker("worker-1", cpu=99.0, age_s=99.0, cpu_reserved=0),
        observed_worker("worker-2", cpu=1.0, age_s=99.0, cpu_reserved=1536,
                        active_jobs=1),
    )
    # worker-2's flattering 1% reading is stale and therefore ignored.
    assert outcome.selected_worker == "worker-1"
    assert all(row["telemetry_fresh"] == 0.0 for row in outcome.terms.values())


# --------------------------------------------------------------------------- #
# the Phase 2 blind spot -- the reason this phase exists
# --------------------------------------------------------------------------- #
def test_cpu_bound_job_avoids_a_statically_attractive_but_saturated_worker():
    """Section 18 of the Phase 3 brief, exactly.

    worker-1 has the *best* static availability (lowest reservations) but is
    observed at 99% CPU. worker-2 is slightly worse statically but nearly idle.
    Static picks worker-1; adaptive-v1 must pick worker-2.
    """
    fleet = make_fleet(
        observed_worker("worker-1", cpu=99.0, cpu_reserved=256, memory_reserved=128),
        observed_worker("worker-2", cpu=20.0, cpu_reserved=1024, memory_reserved=512,
                        active_jobs=1),
    )
    job = make_job("prime_count", cpu_units=512, memory_mb=256)

    static = place(fleet, job, strategy="static_resource_aware")
    adaptive = place(fleet, job, strategy="adaptive")

    assert static.selected_worker == "worker-1", "baseline should show the blind spot"
    assert adaptive.selected_worker == "worker-2", "adaptive must see through it"
    assert adaptive.observed_cpu("worker-1") == 99.0


def test_memory_bound_job_avoids_a_memory_pressured_worker():
    fleet = make_fleet(
        observed_worker("worker-1", cpu=10.0, memory=94.0, cpu_reserved=256,
                        memory_reserved=128),
        observed_worker("worker-2", cpu=10.0, memory=15.0, cpu_reserved=1024,
                        memory_reserved=512, active_jobs=1),
    )
    job = make_job("sort_large", workload_class=WorkloadClass.MEMORY_BOUND,
                   cpu_units=512, memory_mb=256)

    assert place(fleet, job, strategy="static_resource_aware").selected_worker == "worker-1"
    assert place(fleet, job, strategy="adaptive").selected_worker == "worker-2"


def test_class_weights_change_the_answer_for_the_same_fleet():
    """A CPU-pressured worker and a memory-pressured worker: which is worse
    depends on the workload class."""
    fleet = make_fleet(
        observed_worker("worker-1", cpu=95.0, memory=10.0),
        observed_worker("worker-2", cpu=10.0, memory=95.0),
    )
    cpu_job = make_job("prime_count", workload_class=WorkloadClass.CPU_BOUND)
    mem_job = make_job("sort_large", workload_class=WorkloadClass.MEMORY_BOUND)

    assert place(fleet, cpu_job).selected_worker == "worker-2"
    assert place(fleet, mem_job).selected_worker == "worker-1"


# --------------------------------------------------------------------------- #
# hard constraints and continuity
# --------------------------------------------------------------------------- #
def test_telemetry_cannot_make_an_infeasible_worker_eligible():
    outcome = select(
        make_job(cpu_units=1024, memory_mb=256),
        observed_worker("worker-1", cpu=0.0, cpu_reserved=2048),          # no CPU left
        observed_worker("worker-2", cpu=0.0, slots=2, active_jobs=2),     # no slot
        observed_worker("worker-3", cpu=99.0, cpu_reserved=0),            # feasible, busy
    )
    assert outcome.selected_worker == "worker-3"
    assert set(outcome.excluded) == {"worker-1", "worker-2"}


def test_telemetry_cannot_make_a_feasible_worker_ineligible():
    outcome = select(
        make_job(cpu_units=256, memory_mb=256),
        observed_worker("worker-1", cpu=100.0, memory=100.0),
    )
    assert outcome.selected_worker == "worker-1"
    assert outcome.excluded == {}


def test_unhealthy_worker_is_excluded_regardless_of_telemetry():
    outcome = select(
        make_job(cpu_units=256, memory_mb=256),
        observed_worker("worker-1", cpu=1.0, status=WorkerStatus.UNHEALTHY),
        observed_worker("worker-2", cpu=95.0),
    )
    assert outcome.selected_worker == "worker-2"
    assert outcome.excluded["worker-1"] == "worker not healthy"


def test_scoring_is_continuous_with_no_threshold_cliff():
    """Section 17: no `if cpu > 90: never_use()` rules.

    Score must move smoothly through the region where a naive implementation
    would put a cliff.
    """
    job = make_job()
    weights = AdaptivePlacement.weights_for(job.workload_class)

    def score_at(cpu: float) -> float:
        return AdaptivePlacement.score(
            components(job, observed_worker("w", cpu=cpu)), weights
        )

    below, above = score_at(89.9), score_at(90.1)
    assert above > below
    assert above - below == pytest.approx(weights["cpu"] * 0.002, abs=1e-9)

    # And monotonic across the whole range.
    scores = [score_at(c) for c in range(0, 101, 5)]
    assert scores == sorted(scores)


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_identical_workers_break_ties_deterministically():
    fleet = make_fleet(
        observed_worker("worker-1", cpu=40.0),
        observed_worker("worker-2", cpu=40.0),
        observed_worker("worker-3", cpu=40.0),
    )
    assert {place(fleet).selected_worker for _ in range(20)} == {"worker-1"}


def test_tie_break_ignores_input_order():
    a = observed_worker("worker-1", cpu=40.0)
    b = observed_worker("worker-2", cpu=40.0)
    c = observed_worker("worker-3", cpu=40.0)
    for ordering in ((a, b, c), (c, b, a), (b, c, a)):
        assert select(make_job(), *ordering).selected_worker == "worker-1"


def test_repeated_scoring_is_bit_identical():
    fleet = make_fleet(
        observed_worker("worker-1", cpu=37.5, trend=1.25, cpu_reserved=333),
        observed_worker("worker-2", cpu=61.25, trend=-2.5, cpu_reserved=777),
    )
    first = place(fleet)
    second = place(fleet)
    assert first.candidates["worker-1"].score == second.candidates["worker-1"].score
    assert first.selected_worker == second.selected_worker


# --------------------------------------------------------------------------- #
# trace contents
# --------------------------------------------------------------------------- #
def test_trace_records_every_component_not_just_the_final_score():
    decision = place(
        make_fleet(observed_worker("worker-1", cpu=61.0, memory=22.0, trend=2.0))
    )
    row = decision.candidates["worker-1"].terms
    for name in COMPONENTS:
        assert name in row, name
        assert f"contrib_{name}" in row, name
    assert sum(row[f"contrib_{n}"] for n in COMPONENTS) == pytest.approx(
        decision.candidates["worker-1"].score
    )


def test_trace_records_telemetry_quality():
    decision = place(
        make_fleet(
            observed_worker("worker-1", cpu=61.0, age_s=0.4, samples=3),
            observed_worker("worker-2", cpu=5.0, age_s=42.0),
        )
    )
    assert decision.candidates["worker-1"].telemetry_fresh is True
    assert decision.candidates["worker-2"].telemetry_fresh is False
    assert decision.candidates["worker-1"].observed_window.samples == 3
    assert decision.candidates["worker-1"].observed_age_s == 0.4


def test_trace_records_the_algorithm_version():
    decision = place(make_fleet(observed_worker("worker-1", cpu=10.0)))
    assert decision.algorithm_version == "adaptive-v1"
    assert decision.placement_policy == "adaptive"
    assert decision.ordering_policy == "fifo"


def test_static_traces_carry_their_own_version():
    fleet = make_fleet(observed_worker("worker-1", cpu=10.0))
    assert place(fleet, strategy="static_resource_aware").algorithm_version == "static-v1"
    assert place(fleet, strategy="fifo").algorithm_version == "round-robin-v1"


def test_freshness_threshold_reaches_the_policy_through_the_registry():
    """Regression: the strategy registry hardcoded a 6 s threshold for adaptive
    and special-cased it by name inside build_scheduler, so a configured value
    was silently ignored and adding another telemetry-aware strategy would have
    reintroduced the bug."""
    strategy = build_scheduler("adaptive", telemetry_max_age_s=1.5)
    assert strategy.placement.telemetry_max_age_s == 1.5
    assert strategy.telemetry_max_age_s == 1.5

    view = strategy.placement.build_views(
        (observed_worker("w", cpu=10.0, age_s=2.0),)
    )[0]
    assert view.telemetry_fresh is False, "2.0s must be stale under a 1.5s threshold"


def test_every_registered_strategy_uses_fifo_ordering():
    """The experimental control: strategies differ in placement only."""
    from scheduler import STRATEGIES

    for name in STRATEGIES:
        assert build_scheduler(name).ordering.name == "fifo", name
