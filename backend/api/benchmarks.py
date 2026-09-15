"""Benchmark endpoints -- the Phase 7 deliverable.

Exit criterion: ``POST /benchmarks`` produces an archived ``raw.jsonl`` and a
report. Both are here, and both are read back out of the archive rather than
held in memory, so a report survives a restart and a run archived by the CLI is
indistinguishable from one archived through the API.

``POST /benchmarks`` returns 202 with a benchmark id. A sweep takes minutes and
runs are executed strictly one at a time -- concurrent runs would pin two fleets
to the same cores and each would measure the other's interference
(backend/services/benchmarks.py).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from backend.api.deps import get_benchmarks, get_engine
from backend.api.schemas import BenchmarkSubmitRequest
from backend.services.benchmarks import BenchmarkRequest, BenchmarkService
from backend.services.engine import NimbusEngine

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


def _artifact_root(service: BenchmarkService) -> str:
    return f"{service.artifact_dir.rstrip('/')}/benchmarks"


@router.get("/profiles")
def list_profiles() -> list[dict[str, Any]]:
    """The workload profile catalogue.

    A profile plus a seed plus a job count fully determines the job sequence,
    which is what lets two strategies be compared on a literally identical
    workload (docs/experiments.md section 2.1). The pressure specs are listed
    because external pressure is the whole point of profiles B and C: it is
    applied *outside* Nimbus, so it never appears as a reservation and the
    static policies genuinely cannot see it.
    """
    from benchmark.profiles import PROFILES

    return [
        {
            "name": profile.name,
            "description": profile.description,
            "arrival": profile.arrival,
            "rate_per_s": profile.rate_per_s,
            "size_distribution": profile.size_distribution,
            "mix": {k.value: round(v, 3) for k, v in profile.normalised_mix().items()},
            "pressure": [
                {
                    "kind": spec.kind,
                    "target_worker": spec.target_worker,
                    "intensity": spec.intensity,
                }
                for spec in profile.pressure
            ],
        }
        for profile in PROFILES.values()
    ]


@router.post("", status_code=202)
def submit_benchmark(
    req: BenchmarkSubmitRequest,
    service: BenchmarkService = Depends(get_benchmarks),
    engine: NimbusEngine = Depends(get_engine),
) -> dict[str, Any]:
    """Queue a sweep. Returns immediately with a benchmark id to poll."""
    try:
        run = service.submit(
            BenchmarkRequest(
                profile=req.profile,
                strategies=list(req.strategies),
                job_count=req.job_count,
                trials=req.trials,
                seed=req.seed,
                telemetry=req.telemetry,
                size_scale=req.size_scale,
                worker_count=req.worker_count or engine.config.worker_count,
                worker_slots=req.worker_slots or engine.config.worker_slots,
                warmup_jobs=req.warmup_jobs,
                timeout_s=req.timeout_s,
                apply_pressure=req.apply_pressure,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return run.as_dict()


@router.get("")
def list_benchmarks(
    limit: int = Query(50, ge=1, le=200),
    service: BenchmarkService = Depends(get_benchmarks),
) -> list[dict[str, Any]]:
    """Sweeps submitted through this process, newest first."""
    return [run.as_dict() for run in service.list(limit=limit)]


@router.get("/runs")
def list_runs(
    profile: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    service: BenchmarkService = Depends(get_benchmarks),
) -> list[dict[str, Any]]:
    """Every archived run, whoever produced it.

    Read from the artifact store rather than from the service's registry, so a
    run archived by ``scripts/run_phase3_experiments.py`` shows up here exactly
    like one submitted through the API.
    """
    from benchmark.report import load_runs, summarise_runs

    runs = load_runs(_artifact_root(service), profile=profile)
    return list(reversed(summarise_runs(runs)))[:limit]


@router.get("/report")
def report(
    profile: str,
    treatment: str = "adaptive",
    metric: str = "makespan_s",
    fmt: str = Query("json", pattern="^(json|markdown)$"),
    service: BenchmarkService = Depends(get_benchmarks),
) -> Any:
    """Derive a profile's report from the archive.

    Nothing is cached. Every figure is recomputed from
    ``benchmarks/{run_id}/`` on each request, which is the property
    docs/experiments.md requires: a number in the write-up must be regenerable
    from the archive alone. A cache would let a stale figure outlive the data
    it came from.
    """
    from benchmark.report import build_report

    markdown, payload = build_report(
        _artifact_root(service), profile=profile, treatment=treatment, metric=metric
    )
    if payload["runs_total"] == 0:
        raise HTTPException(status_code=404, detail=f"no archived runs for profile {profile!r}")
    if fmt == "markdown":
        return Response(content=markdown, media_type="text/markdown")
    return {**payload, "markdown": markdown}


@router.get("/{benchmark_id}")
def get_benchmark(
    benchmark_id: str, service: BenchmarkService = Depends(get_benchmarks)
) -> dict[str, Any]:
    run = service.get(benchmark_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no such benchmark: {benchmark_id}")
    return run.as_dict()


@router.post("/{benchmark_id}/cancel")
def cancel_benchmark(
    benchmark_id: str, service: BenchmarkService = Depends(get_benchmarks)
) -> dict[str, Any]:
    """Stop a sweep at the next cell boundary.

    Not mid-cell: a partially executed cell would archive a truncated run, and
    a truncated run reports an ordinary-looking makespan for a workload it did
    not finish. Every archived run is whole or absent.
    """
    if not service.cancel(benchmark_id):
        raise HTTPException(
            status_code=409,
            detail=f"{benchmark_id} is unknown or already finished",
        )
    return service.get(benchmark_id).as_dict()


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    service: BenchmarkService = Depends(get_benchmarks),
) -> dict[str, Any]:
    """One archived run's manifest and metrics.

    The manifest is the reproducibility record: seed, profile, strategy,
    algorithm version, git sha, host, and the full job plan. A number without a
    manifest is not a result (docs/experiments.md section 2.4).
    """
    from benchmark.report import load_runs

    for run in load_runs(_artifact_root(service)):
        if run.run_id == run_id:
            return {
                "run_id": run.run_id,
                "manifest": run.manifest,
                "metrics": run.metrics,
                "truncated": run.truncated,
                "load_regime_ok": (run.value("mean_fleet_utilisation") or 0.0) >= 0.50,
            }
    raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
