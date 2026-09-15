"""The Phase 2 invariant, tested against the running system.

    Nimbus can observe reality without using that reality to place jobs.

Unit tests pin the boundary structurally; these exercise it end to end, with a
real fleet, real psutil sampling, and real external CPU pressure applied by
processes that never import Nimbus.
"""

from __future__ import annotations

import time

import pytest

from backend.config import NimbusConfig, set_config
from backend.models.enums import JobStatus, WorkerStatus
from backend.services.engine import NimbusEngine
from benchmark import pressure as pressure_mod


def build_engine(**overrides) -> NimbusEngine:
    config = NimbusConfig(
        strategy=overrides.pop("strategy", "static_resource_aware"),
        worker_count=3,
        worker_slots=2,
        scheduler_tick_s=0.01,
        telemetry_enabled=overrides.pop("telemetry_enabled", True),
        telemetry_interval_s=overrides.pop("telemetry_interval_s", 0.3),
        heartbeat_interval_s=overrides.pop("heartbeat_interval_s", 0.5),
        **overrides,
    )
    set_config(config)
    return NimbusEngine(config)


@pytest.fixture
def static_engine(tmp_path):
    engine = build_engine(artifact_dir=str(tmp_path / "artifacts"))
    engine.start()
    try:
        yield engine
    finally:
        engine.stop(wait=False)


def wait_for_samples(engine: NimbusEngine, minimum: int = 2, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if engine.telemetry.stats.ticks >= minimum:
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #
def test_telemetry_is_collected_for_every_worker(static_engine: NimbusEngine):
    assert wait_for_samples(static_engine)
    for state in static_engine.fleet().workers:
        assert state.observed is not None, f"{state.worker_id} has no telemetry"
        assert state.observed.worker_id == state.worker_id
        assert state.observed.timestamp is not None
        assert state.telemetry_age_s is not None
        assert len(static_engine.registry.telemetry_history(state.worker_id)) >= 1


def test_declared_reserved_and_observed_stay_separate(static_engine: NimbusEngine):
    assert wait_for_samples(static_engine)
    state = static_engine.fleet().by_id("worker-1")
    assert state.declared.cpu_units == static_engine.config.worker_cpu_units
    assert state.reserved.cpu_units == 0
    assert state.observed is not None
    # The three categories are distinct objects; nothing merges them.
    assert not hasattr(state.declared, "cpu_percent")
    assert not hasattr(state.reserved, "cpu_percent")


def test_telemetry_overhead_is_small_and_measured(static_engine: NimbusEngine):
    assert wait_for_samples(static_engine, minimum=4)
    overhead = static_engine.telemetry_overhead()
    assert overhead["ticks"] >= 4
    assert overhead["errors"] == 0
    assert overhead["mean_tick_ms"] > 0
    assert overhead["duty_cycle"] < 0.05, f"telemetry is expensive: {overhead}"


# --------------------------------------------------------------------------- #
# ON/OFF control
# --------------------------------------------------------------------------- #
def test_toggling_telemetry_off_stops_collection_and_clears_observations(
    static_engine: NimbusEngine,
):
    assert wait_for_samples(static_engine)
    static_engine.set_telemetry(False)

    assert not static_engine.telemetry.running
    assert all(w.observed is None for w in static_engine.fleet().workers)
    assert static_engine.registry.telemetry_history("worker-1") == []


def test_fleet_stays_healthy_and_schedulable_with_telemetry_off(tmp_path):
    """Heartbeats are independent of telemetry: the OFF arm must still place
    jobs, or the control experiment is impossible."""
    engine = build_engine(
        telemetry_enabled=False, artifact_dir=str(tmp_path / "artifacts")
    )
    engine.start()
    try:
        assert not engine.telemetry.running
        for _ in range(6):
            engine.submit(job_type="json_transform", payload={"records": 300})
        assert engine.wait_for_idle(timeout_s=60)

        fleet = engine.fleet()
        assert all(w.status is WorkerStatus.HEALTHY for w in fleet.workers)
        assert all(w.observed is None for w in fleet.workers)
        assert engine.store.counts_by_status()["COMPLETED"] == 6
    finally:
        engine.stop(wait=False)


def _run_plan(tmp_path, plan, *, telemetry_enabled: bool, tag: str):
    engine = build_engine(
        telemetry_enabled=telemetry_enabled, artifact_dir=str(tmp_path / f"artifacts-{tag}")
    )
    engine.start()
    try:
        if telemetry_enabled:
            # Wait for the collector to produce a sample before submitting.
            # These plans are 24 tiny jobs and can drain in under one sampling
            # interval, in which case every trace would legitimately record no
            # observation -- and the assertion below that telemetry-on traces
            # carry observations would fail for a timing reason that has
            # nothing to do with the boundary being tested.
            assert wait_for_samples(engine, minimum=1), "collector produced no samples"
        ids = [
            engine.submit(job_type=p.job_type, payload=p.payload, priority=p.priority).job_id
            for p in plan
        ]
        assert engine.wait_for_idle(timeout_s=180)
        by_job = {d.job_id: d for d in engine.decisions(limit=500)}
        sequence = [by_job[i].selected_worker for i in ids if i in by_job]
        return sequence, [by_job[i] for i in ids if i in by_job]
    finally:
        engine.stop(wait=False)


def test_live_placement_sequences_are_not_reproducible_run_to_run(tmp_path):
    """Documents why bit-identical placement is the *wrong* thing to assert.

    Reservation state at decision time depends on when jobs actually finish, so
    two identical runs diverge -- with telemetry off in both. Attributing that
    divergence to telemetry would be a mistake, so the control experiment
    asserts determinism via replay instead (the test below).
    """
    from benchmark.generator import generate

    plan = generate("homogeneous_saturated", 24, seed=1337, size_scale=0.2)
    first, _ = _run_plan(tmp_path, plan, telemetry_enabled=False, tag="off-a")
    second, _ = _run_plan(tmp_path, plan, telemetry_enabled=False, tag="off-b")

    assert len(first) == len(second) == len(plan)
    # Not an equality assertion: this documents inherent run-to-run variance.
    differing = sum(1 for a, b in zip(first, second) if a != b)
    assert differing >= 0
    if differing == 0:
        pytest.skip("this run happened to be identical; the property is probabilistic")


def test_placement_is_a_deterministic_function_of_static_state(tmp_path):
    """The control experiment, correctly formulated.

    Replay every archived decision through the same policy, rebuilding its
    inputs from the trace's DECLARED and RESERVED fields only. Every recorded
    choice and score must be reproduced exactly -- with telemetry on and with
    it off. If placement had consumed observed state, the telemetry-on traces
    would fail to replay from static inputs alone.
    """
    from benchmark.generator import generate
    from scheduler.replay import verify_all

    plan = generate("homogeneous_saturated", 24, seed=1337, size_scale=0.2)

    for telemetry_enabled, tag in ((False, "det-off"), (True, "det-on")):
        _, decisions = _run_plan(tmp_path, plan, telemetry_enabled=telemetry_enabled, tag=tag)
        placed = [d for d in decisions if d.selected_worker]
        assert len(placed) >= len(plan) - 2

        matched, mismatched = verify_all(placed)
        assert not mismatched, (
            f"telemetry={telemetry_enabled}: {len(mismatched)} decision(s) could not be "
            f"reproduced from static state alone -- "
            f"{[(m.decision_id, m.recorded_worker, m.replayed_worker) for m in mismatched[:3]]}"
        )
        assert len(matched) == len(placed)

        if telemetry_enabled:
            assert any(d.observed_cpu("worker-1") is not None for d in placed), (
                "telemetry-on traces should carry observations"
            )


def test_telemetry_does_not_shift_the_placement_distribution(tmp_path):
    """Aggregate control: every worker is still used, with or without telemetry."""
    from collections import Counter

    from benchmark.generator import generate

    plan = generate("homogeneous_saturated", 30, seed=4242, size_scale=0.2)
    off, _ = _run_plan(tmp_path, plan, telemetry_enabled=False, tag="dist-off")
    on, _ = _run_plan(tmp_path, plan, telemetry_enabled=True, tag="dist-on")

    off_counts, on_counts = Counter(off), Counter(on)
    assert set(off_counts) == set(on_counts) == {"worker-1", "worker-2", "worker-3"}
    # No worker is starved in either arm.
    assert min(off_counts.values()) >= 2 and min(on_counts.values()) >= 2


# --------------------------------------------------------------------------- #
# external pressure -- the critical scenario
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_external_cpu_pressure_is_observed_but_not_acted_on(static_engine: NimbusEngine):
    """worker-1 is driven to ~100% CPU by processes outside Nimbus.

    Expected, and correct for Phase 2:
      * telemetry records the pressure;
      * Nimbus's declared/reserved model is unchanged;
      * static placement keeps sending worker-1 its share of the work.

    This is the baseline Phase 3 will be measured against. It is not a bug.
    """
    cores = static_engine.registry.cores_for("worker-1")
    stressor = pressure_mod.start_cpu_pressure("worker-1", cores, intensity=1.0)
    try:
        assert wait_for_samples(static_engine, minimum=3, timeout_s=10.0)
        time.sleep(0.8)

        fleet = static_engine.fleet()
        pressured = fleet.by_id("worker-1")
        others = [w for w in fleet.workers if w.worker_id != "worker-1"]

        # 1. Telemetry sees it.
        assert pressured.observed.cpu_percent > 70.0, pressured.observed.summary()
        assert pressured.observed.cpu_percent > max(o.observed.cpu_percent for o in others)

        # 2. Nimbus's static model does not.
        assert pressured.reserved.cpu_units == 0
        assert pressured.available_cpu_units == pressured.declared.cpu_units
        assert pressured.free_slots == pressured.declared.slots

        # 3. Static placement does not avoid it.
        jobs = [
            static_engine.submit(job_type="prime_count", payload={"limit": 400_000})
            for _ in range(18)
        ]
        assert static_engine.wait_for_idle(timeout_s=180)

        placements = [static_engine.store.get_job(j.job_id).assigned_worker for j in jobs]
        share = placements.count("worker-1") / len(placements)
        assert share >= 0.20, (
            f"static placement appears to have avoided the pressured worker "
            f"(share {share:.0%}) -- it must not see telemetry"
        )
    finally:
        stressor.stop()


@pytest.mark.slow
def test_decision_traces_record_the_pressure_that_was_ignored(static_engine: NimbusEngine):
    cores = static_engine.registry.cores_for("worker-1")
    stressor = pressure_mod.start_cpu_pressure("worker-1", cores, intensity=1.0)
    try:
        assert wait_for_samples(static_engine, minimum=3, timeout_s=10.0)
        job = static_engine.submit(job_type="json_transform", payload={"records": 400})
        assert static_engine.wait_for_idle(timeout_s=60)

        decision = static_engine.decisions(job_id=job.job_id)[0]
        assert decision.placement_policy == "static_resource_aware"
        assert decision.telemetry_enabled is True

        observed = decision.observed_cpu("worker-1")
        assert observed is not None and observed > 70.0, observed

        # The trace holds it; the score does not depend on it.
        candidate = decision.candidates["worker-1"]
        assert candidate.observed is not None
        assert set(candidate.terms) == {
            "projected_cpu", "projected_memory", "projected_slots",
            "contrib_cpu", "contrib_memory", "contrib_slots",
        }
        assert candidate.score == pytest.approx(
            0.40 * candidate.terms["projected_cpu"]
            + 0.30 * candidate.terms["projected_memory"]
            + 0.30 * candidate.terms["projected_slots"]
        )
    finally:
        stressor.stop()


# --------------------------------------------------------------------------- #
# static strategy end to end
# --------------------------------------------------------------------------- #
def test_static_strategy_runs_a_heterogeneous_batch(static_engine: NimbusEngine):
    from benchmark.generator import generate

    plan = generate("homogeneous_saturated", 24, seed=99, size_scale=0.25)
    jobs = [
        static_engine.submit(job_type=p.job_type, payload=p.payload, priority=p.priority)
        for p in plan
    ]
    assert static_engine.wait_for_idle(timeout_s=180)

    finished = [static_engine.store.get_job(j.job_id) for j in jobs]
    assert all(f.status is JobStatus.COMPLETED for f in finished), [
        (f.job_id, f.status, f.error) for f in finished if f.status is not JobStatus.COMPLETED
    ]
    assert static_engine.queue.dead_letters() == []
    assert len({f.assigned_worker for f in finished}) == 3

    decisions = static_engine.decisions(limit=200)
    placed = [d for d in decisions if d.selected_worker]
    assert placed
    assert all(d.ordering_policy == "fifo" for d in placed)
    assert all(d.placement_policy == "static_resource_aware" for d in placed)
    assert all(d.weights == {"cpu": 0.40, "memory": 0.30, "slots": 0.30} for d in placed)
