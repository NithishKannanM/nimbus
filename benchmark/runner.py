"""Experiment orchestration.

One call runs one (profile x strategy x telemetry) cell: build a fresh engine,
generate the seeded job plan, start any *external* host pressure, submit on the
planned arrival schedule, drain, and archive everything needed to reproduce and
re-analyse the run.

Phase 2 scope: establish baselines and run the telemetry ON/OFF control. It does
not try to prove anything about adaptive scheduling, which does not exist yet.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from backend.config import NimbusConfig
from backend.models.decision import SchedulingDecision
from backend.models.job import Job
from backend.models.telemetry import WorkerTelemetry
from backend.services.engine import NimbusEngine
from benchmark import pressure as pressure_mod
from benchmark.generator import PlannedJob, class_histogram, generate
from benchmark.manifest import ExperimentManifest
from benchmark.metrics import RunMetrics, summarise
from benchmark.profiles import Profile, get_profile

log = logging.getLogger("nimbus.benchmark")

WARMUP_PAYLOAD = {"records": 200}


@dataclass
class RunResult:
    manifest: ExperimentManifest
    metrics: RunMetrics
    jobs: list[Job] = field(default_factory=list)
    decisions: list[SchedulingDecision] = field(default_factory=list)
    telemetry: dict[str, list[WorkerTelemetry]] = field(default_factory=dict)
    artifact_prefix: str | None = None
    pressure_applied: list[str] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.manifest.experiment_id


def _submit_on_schedule(
    engine: NimbusEngine, plan: Sequence[PlannedJob], run_id: str
) -> list[str]:
    """Submit jobs following their planned arrival offsets."""
    job_ids: list[str] = []
    started = time.monotonic()
    for planned in plan:
        due = started + planned.arrival_offset_s
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        job = engine.submit(
            job_type=planned.job_type,
            payload=planned.payload,
            priority=planned.priority,
            run_id=run_id,
        )
        job_ids.append(job.job_id)
    return job_ids


def _warm_up(engine: NimbusEngine, run_id: str, count: int) -> None:
    """Discarded jobs so page cache, pool children and interpreter state are
    comparable between the first and last strategy measured."""
    if count <= 0:
        return
    for _ in range(count):
        engine.submit(
            job_type="json_transform", payload=dict(WARMUP_PAYLOAD),
            run_id=f"{run_id}-warmup",
        )
    engine.wait_for_idle(timeout_s=60)


def run_experiment(
    *,
    profile: Profile | str,
    strategy: str = "static_resource_aware",
    job_count: int = 60,
    seed: int = 1337,
    telemetry: bool = True,
    telemetry_interval_s: float = 2.0,
    size_scale: float = 1.0,
    worker_count: int = 3,
    worker_slots: int = 2,
    warmup_jobs: int = 3,
    trial: int = 0,
    timeout_s: float = 600.0,
    artifact_dir: str | None = None,
    apply_pressure: bool = True,
    engine: NimbusEngine | None = None,
) -> RunResult:
    profile = get_profile(profile) if isinstance(profile, str) else profile
    plan = generate(profile, job_count, seed, size_scale=size_scale)

    config = NimbusConfig(
        strategy=strategy,
        worker_count=worker_count,
        worker_slots=worker_slots,
        telemetry_enabled=telemetry,
        telemetry_interval_s=telemetry_interval_s,
        seed=seed,
        **({"artifact_dir": artifact_dir} if artifact_dir else {}),
    )

    owns_engine = engine is None
    engine = engine or NimbusEngine(config)
    if owns_engine:
        engine.start()

    manifest = ExperimentManifest(
        seed=seed,
        ordering=engine.scheduler.ordering.name,
        placement=engine.scheduler.placement.name,
        strategy=engine.scheduler.name,
        algorithm_version=engine.scheduler.placement.algorithm_version,
        telemetry=telemetry,
        telemetry_interval_s=telemetry_interval_s,
        workers=worker_count,
        worker_slots=worker_slots,
        worker_cores={
            w: list(engine.registry.cores_for(w)) for w in engine.registry.worker_ids()
        },
        declared_cpu_units=config.worker_cpu_units,
        declared_memory_mb=config.worker_memory_mb,
        profile=profile.name,
        profile_description=profile.description,
        job_count=job_count,
        size_scale=size_scale,
        pressure_backend=pressure_mod.backend_name() if profile.pressure else None,
        trial=trial,
        config=config.model_dump(),
        job_plan=[p.as_dict() for p in plan],
    )
    run_id = manifest.experiment_id

    stressors: list[pressure_mod.ExternalPressure] = []
    described: list[str] = []
    try:
        _warm_up(engine, run_id, warmup_jobs)

        if apply_pressure:
            for spec in profile.pressure:
                cores = engine.registry.cores_for(spec.target_worker)
                stressor = pressure_mod.start(spec, cores)
                stressors.append(stressor)
                described.append(stressor.describe())
                manifest.pressure.append(
                    {
                        "kind": spec.kind,
                        "target_worker": spec.target_worker,
                        "intensity": spec.intensity,
                        "cores": list(cores),
                        "backend": stressor.backend,
                        "processes": len(stressor.processes),
                    }
                )
            if stressors:
                # Let the stressors reach steady state and telemetry observe it
                # before the measured workload starts.
                time.sleep(max(1.0, telemetry_interval_s))

        _submit_on_schedule(engine, plan, run_id)
        drained = engine.wait_for_idle(timeout_s=timeout_s)
        if not drained:
            log.warning("run %s did not drain within %.0fs", run_id, timeout_s)
    finally:
        for stressor in stressors:
            stressor.stop()

    jobs = engine.store.list_jobs(run_id=run_id, limit=job_count * 4)
    jobs.sort(key=lambda j: j.created_at)
    decisions = [
        d for d in engine.decisions(limit=job_count * 8)
        if d.job_id in {j.job_id for j in jobs}
    ]
    telemetry_by_worker = {
        worker_id: engine.registry.telemetry_history(worker_id, limit=10_000)
        for worker_id in engine.registry.worker_ids()
    }

    metrics = summarise(
        jobs,
        decisions,
        slots_by_worker={w: worker_slots for w in engine.registry.worker_ids()},
        telemetry_by_worker=telemetry_by_worker,
        dead_letters=len(engine.queue.dead_letters()),
        telemetry_overhead=engine.telemetry_overhead(),
        telemetry_interval_s=telemetry_interval_s,
    )
    manifest.finished_at = datetime.now(timezone.utc).isoformat()

    prefix = _archive(engine, run_id, manifest, metrics, jobs, decisions, telemetry_by_worker)

    if owns_engine:
        engine.stop(wait=False)

    return RunResult(
        manifest=manifest,
        metrics=metrics,
        jobs=jobs,
        decisions=decisions,
        telemetry=telemetry_by_worker,
        artifact_prefix=prefix,
        pressure_applied=described,
    )


def _archive(engine, run_id, manifest, metrics, jobs, decisions, telemetry) -> str:
    """Write the run to the artifact store, in the layout documented in
    docs/architecture.md section 9."""
    base = f"benchmarks/{run_id}"
    engine.artifacts.put_json(f"{base}/manifest.json", manifest.as_dict())
    engine.artifacts.put_json(f"{base}/metrics.json", metrics.as_dict())
    engine.artifacts.put_bytes(
        f"{base}/raw.jsonl",
        "\n".join(j.model_dump_json() for j in jobs).encode() + b"\n",
    )
    engine.artifacts.put_bytes(
        f"{base}/decisions.jsonl",
        "\n".join(d.model_dump_json() for d in decisions).encode() + b"\n",
    )
    engine.artifacts.put_bytes(
        f"{base}/telemetry.jsonl",
        "\n".join(
            s.model_dump_json() for samples in telemetry.values() for s in samples
        ).encode() + b"\n",
    )
    return base


def compare_telemetry_control(
    *,
    profile: Profile | str = "homogeneous_saturated",
    job_count: int = 60,
    seed: int = 1337,
    strategy: str = "static_resource_aware",
    size_scale: float = 1.0,
    trials: int = 1,
    **kwargs: Any,
) -> dict[str, Any]:
    """Telemetry ON/OFF control experiment.

    Same profile, same seed, same strategy, so both arms run a literally
    identical job sequence. Two things are checked, and they are different
    claims:

    * **Placement determinism** -- every archived decision must replay exactly
      from its recorded DECLARED and RESERVED state. This is the strong claim,
      and it holds per decision.

    * **Throughput cost** -- may legitimately differ, so it is measured. A
      single run per arm cannot distinguish overhead from noise, which is why
      ``trials`` defaults to being reported with its spread rather than as a
      point estimate.

    Note what is deliberately *not* asserted: that the two arms produce the same
    placement *sequence*. They do not, and neither do two consecutive runs of
    the same arm -- reservation state at decision time depends on real
    completion timing. See tests/integration/test_telemetry_isolation.py.
    """
    from scheduler.replay import verify_all

    arms: dict[str, list[RunResult]] = {"off": [], "on": []}
    # Alternate arms so a machine that warms up or cools down over the sequence
    # does not systematically favour one of them.
    for trial in range(trials):
        order = ("off", "on") if trial % 2 == 0 else ("on", "off")
        for arm in order:
            arms[arm].append(
                run_experiment(
                    profile=profile, strategy=strategy, job_count=job_count, seed=seed,
                    telemetry=(arm == "on"), size_scale=size_scale, trial=trial, **kwargs,
                )
            )

    def throughputs(arm: str) -> list[float]:
        return sorted(r.metrics.throughput_jobs_s for r in arms[arm])

    replay_mismatches = 0
    replay_checked = 0
    for arm_results in arms.values():
        for result in arm_results:
            replayable = [
                d for d in result.decisions
                if d.selected_worker and d.placement_policy.startswith("static_resource_aware")
            ]
            matched, mismatched = verify_all(replayable)
            replay_checked += len(replayable)
            replay_mismatches += len(mismatched)

    off_thr, on_thr = throughputs("off"), throughputs("on")
    median = lambda xs: statistics.median(xs) if xs else 0.0
    delta = ((median(on_thr) - median(off_thr)) / median(off_thr) * 100) if median(off_thr) else 0.0

    return {
        "trials": trials,
        "replay_checked": replay_checked,
        "replay_mismatches": replay_mismatches,
        "placement_deterministic": replay_mismatches == 0,
        "off": arms["off"],
        "on": arms["on"],
        "throughput_off": off_thr,
        "throughput_on": on_thr,
        "throughput_delta_pct": round(delta, 2),
        "overhead": arms["on"][-1].metrics.telemetry_overhead if arms["on"] else {},
    }


def load_run(artifact_root: str, run_id: str) -> dict[str, Any]:
    """Re-read an archived run. Every reported figure must be regenerable from
    these files."""
    from pathlib import Path

    base = Path(artifact_root) / "benchmarks" / run_id
    return {
        "manifest": json.loads((base / "manifest.json").read_text()),
        "metrics": json.loads((base / "metrics.json").read_text()),
        "jobs": [json.loads(line) for line in (base / "raw.jsonl").read_text().splitlines() if line],
        "decisions": [
            json.loads(line) for line in (base / "decisions.jsonl").read_text().splitlines() if line
        ],
    }


__all__ = [
    "RunResult",
    "run_experiment",
    "compare_telemetry_control",
    "load_run",
    "class_histogram",
]
