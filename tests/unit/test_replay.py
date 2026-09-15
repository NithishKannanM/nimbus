"""Decision-trace replay.

A trace that cannot reproduce its own decision is not a trace, it is a log. The
replay path is also how the telemetry boundary is verified end to end: if a
policy consumed observed state, its decisions would not reproduce from the
recorded DECLARED and RESERVED fields alone.
"""

from __future__ import annotations

import pytest

from backend.models.workload_helpers import make_job
from scheduler import build_scheduler
from scheduler.placement.static_resource_aware import StaticResourceAwarePlacement
from scheduler.replay import (
    NotReplayable,
    replay,
    requirements,
    static_views,
    verify,
    verify_all,
)
from tests.conftest import make_fleet, telemetry, worker_state


def a_decision(strategy="static_resource_aware", *, with_telemetry=True, job=None):
    fleet = make_fleet(
        worker_state(
            "worker-1", cpu_reserved=1536, memory_reserved=1024, active_jobs=1,
            observed=telemetry("worker-1", cpu=97.0) if with_telemetry else None,
        ),
        worker_state(
            "worker-2", cpu_reserved=256, memory_reserved=256, active_jobs=0,
            observed=telemetry("worker-2", cpu=4.0) if with_telemetry else None,
        ),
        worker_state(
            "worker-3", cpu_reserved=512, memory_reserved=512, active_jobs=1,
            observed=telemetry("worker-3", cpu=50.0) if with_telemetry else None,
        ),
    )
    return build_scheduler(strategy).place(job or make_job(cpu_units=512, memory_mb=512), fleet)


def test_replay_reproduces_the_recorded_choice():
    result = verify(a_decision())
    assert result.matches
    assert result.recorded_worker == result.replayed_worker == "worker-2"


def test_replay_reproduces_every_score_exactly():
    decision = a_decision()
    outcome = replay(decision)
    for worker_id, candidate in decision.candidates.items():
        if candidate.score is not None:
            assert outcome.scores[worker_id] == pytest.approx(candidate.score, abs=1e-12)


def test_replay_is_unaffected_by_recorded_telemetry():
    """Same static state, opposite observations -> identical replay."""
    with_t = verify(a_decision(with_telemetry=True))
    without_t = verify(a_decision(with_telemetry=False))
    assert with_t.matches and without_t.matches
    assert with_t.replayed_worker == without_t.replayed_worker


def test_replay_inputs_carry_no_telemetry():
    views = static_views(a_decision())
    assert all(type(v).__name__ == "StaticWorkerView" for v in views)
    assert not any(hasattr(v, "observed") or hasattr(v, "cpu_percent") for v in views)


def test_static_views_rebuild_declared_and_reserved_faithfully():
    views = {v.worker_id: v for v in static_views(a_decision())}
    assert views["worker-1"].cpu_reserved == 1536
    assert views["worker-1"].memory_reserved == 1024
    assert views["worker-1"].slots_used == 1
    assert views["worker-2"].cpu_capacity == 2048
    assert views["worker-2"].memory_capacity == 4096


def test_requirements_are_recovered_from_the_trace():
    job = make_job("matrix_multiply", cpu_units=1024, memory_mb=768, priority=2)
    req = requirements(a_decision(job=job))
    assert (req.job_type, req.cpu_units, req.memory_mb, req.priority) == (
        "matrix_multiply", 1024, 768, 2
    )


def test_health_is_recorded_so_exclusions_replay():
    from backend.models.enums import WorkerStatus

    fleet = make_fleet(
        worker_state("worker-1", status=WorkerStatus.UNHEALTHY),
        worker_state("worker-2", cpu_reserved=1024, active_jobs=1),
    )
    decision = build_scheduler("static_resource_aware").place(make_job(), fleet)
    assert decision.candidates["worker-1"].healthy is False

    outcome = replay(decision)
    assert outcome.selected_worker == decision.selected_worker == "worker-2"
    assert "worker-1" in outcome.excluded


def test_binpack_traces_replay_under_binpack():
    result = verify(a_decision("static_resource_aware_binpack"))
    assert result.matches
    assert result.recorded_worker == "worker-1"


def test_replaying_binpack_trace_with_balance_policy_diverges():
    """A counterfactual: the same trace under a different objective picks
    differently, which is what makes replay useful for Phase 3."""
    decision = a_decision("static_resource_aware_binpack")
    counterfactual = replay(decision, StaticResourceAwarePlacement(mode="balance"))
    assert counterfactual.selected_worker != decision.selected_worker


def test_round_robin_is_not_replayable():
    with pytest.raises(NotReplayable, match="stateful"):
        replay(a_decision("fifo"))


def test_verify_all_partitions_results():
    decisions = [a_decision() for _ in range(5)]
    matched, mismatched = verify_all(decisions)
    assert len(matched) == 5 and mismatched == []


def test_verify_detects_a_tampered_trace():
    """Sanity check that the verifier can actually fail."""
    decision = a_decision()
    tampered = decision.model_copy(update={"selected_worker": "worker-3"})
    assert not verify(tampered).matches


# --------------------------------------------------------------------------- #
# adaptive-v1 replay
# --------------------------------------------------------------------------- #
def an_adaptive_decision(*, stale_worker: str | None = None, job=None):
    from tests.conftest import observed_worker

    def build(worker_id, cpu, **kwargs):
        age = 99.0 if worker_id == stale_worker else 0.5
        return observed_worker(worker_id, cpu=cpu, age_s=age, **kwargs)

    fleet = make_fleet(
        build("worker-1", 97.0, cpu_reserved=256, memory_reserved=128),
        build("worker-2", 21.0, cpu_reserved=1024, memory_reserved=512, active_jobs=1),
        build("worker-3", 44.0, cpu_reserved=512, memory_reserved=256, active_jobs=1),
    )
    return build_scheduler("adaptive", telemetry_max_age_s=6.0).place(
        job or make_job("prime_count", cpu_units=512, memory_mb=256), fleet
    )


def test_adaptive_decision_replays_exactly():
    result = verify(an_adaptive_decision())
    assert result.matches
    assert result.algorithm_version == "adaptive-v1"


def test_adaptive_replay_reproduces_every_component_score():
    decision = an_adaptive_decision()
    outcome = replay(decision)
    for worker_id, candidate in decision.candidates.items():
        if candidate.score is None:
            continue
        assert outcome.scores[worker_id] == pytest.approx(candidate.score, abs=1e-12)


def test_adaptive_replay_survives_a_json_round_trip():
    """Archived traces are JSONL on disk, so replay has to work from the
    serialised form, not just the in-memory object."""
    decision = an_adaptive_decision()
    revived = type(decision).model_validate_json(decision.model_dump_json())
    assert verify(revived).matches


def test_adaptive_replay_preserves_the_recorded_freshness_verdict():
    """Freshness is replayed from the trace, not recomputed against the current
    threshold -- otherwise changing NIMBUS_TELEMETRY_MAX_AGE_SECONDS would
    retroactively alter what an archived decision 'should' have been."""
    decision = an_adaptive_decision(stale_worker="worker-2")
    assert decision.candidates["worker-2"].telemetry_fresh is False
    assert verify(decision).matches

    from scheduler.replay import adaptive_views

    stale = next(v for v in adaptive_views(decision) if v.worker_id == "worker-2")
    assert stale.telemetry_fresh is False


def test_counterfactual_static_replay_of_an_adaptive_trace():
    """The whole point of the trace format: re-score an archived decision under
    a different policy without re-running the workload."""
    decision = an_adaptive_decision()
    counterfactual = replay(decision, StaticResourceAwarePlacement(mode="balance"))
    assert decision.selected_worker != "worker-1"
    assert counterfactual.selected_worker == "worker-1", (
        "static should still be drawn to the lightly-reserved but saturated worker"
    )


def test_counterfactual_adaptive_replay_of_a_static_trace():
    from scheduler.placement.adaptive import AdaptivePlacement
    from tests.conftest import observed_worker

    fleet = make_fleet(
        observed_worker("worker-1", cpu=98.0, cpu_reserved=256, memory_reserved=128),
        observed_worker("worker-2", cpu=15.0, cpu_reserved=1024, memory_reserved=512,
                        active_jobs=1),
    )
    static_decision = build_scheduler("static_resource_aware").place(
        make_job("prime_count", cpu_units=512, memory_mb=256), fleet
    )
    assert static_decision.selected_worker == "worker-1"

    counterfactual = replay(static_decision, AdaptivePlacement())
    assert counterfactual.selected_worker == "worker-2"


def test_adaptive_replay_detects_a_tampered_trace():
    decision = an_adaptive_decision()
    tampered = decision.model_copy(update={"selected_worker": "worker-1"})
    assert not verify(tampered).matches
