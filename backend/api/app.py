from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.api import admin, benchmarks, internal, jobs, scheduler, workers
from backend.config import get_config
from backend.services.benchmarks import BenchmarkService
from backend.services.engine import NimbusEngine

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
)

DESCRIPTION = """
Nimbus -- telemetry-aware adaptive workload scheduling, an *experimental*
distributed-systems project.

AWS already provides production job scheduling (AWS Batch, ECS task placement,
SQS, EC2 Auto Scaling). Nimbus is not a replacement for any of them; see
docs/aws-native-scheduling.md. Phase 2 adds Static Resource-Aware placement and worker telemetry. Telemetry is
collected, recorded in every decision trace and exposed here -- and is
deliberately *not* readable by any implemented placement policy, so that the
Phase 3 adaptive-vs-static comparison has a genuine control.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_config()
    engine = NimbusEngine(config)
    engine.start()
    app.state.engine = engine
    # Benchmark runs build their own engines; this one only owns the queue of
    # them, so it costs nothing until a sweep is actually submitted.
    app.state.benchmarks = BenchmarkService(artifact_dir=config.artifact_dir)
    try:
        yield
    finally:
        app.state.benchmarks.stop()
        engine.stop()


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the built dashboard from the control plane, if it has been built.

    Mounted last and at the root, so every API route already registered wins the
    match and only unclaimed paths fall through to the SPA. Mounting it first
    would shadow the API with a 404-producing static handler.

    Absent silently when ``frontend/dist`` does not exist. The dashboard is a
    separate build with its own toolchain, and the API must start without it --
    every test in this repo, and the whole benchmark harness, runs with no
    frontend built at all.
    """
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not (dist / "index.html").exists():
        return
    # html=True serves index.html for unmatched paths, which is what a
    # client-routed SPA needs.
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="dashboard")
    log = logging.getLogger("nimbus.api")
    log.info("dashboard mounted from %s", dist)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Nimbus",
        description=DESCRIPTION,
        version="0.7.0-phase7",
        lifespan=lifespan,
    )
    app.include_router(jobs.router)
    app.include_router(workers.router)
    app.include_router(scheduler.router)
    app.include_router(benchmarks.router)
    # Mounted only when explicitly enabled. Every route on it is destructive,
    # and Nimbus has no authentication before Phase 7, so not existing is the
    # only access control there is (docs/limitations.md section 5).
    config = get_config()
    if config.enable_admin:
        app.include_router(admin.router)
    # Remote workers POST their results here. Local workers are in-process and
    # push onto a queue directly, so on the local backend this endpoint would
    # serve nothing.
    if config.backend == "aws" or config.worker_transport == "http":
        app.include_router(internal.router)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, Any]:
        engine: NimbusEngine = app.state.engine
        fleet = engine.fleet()
        return {
            "ok": True,
            "phase": 7,
            "backend": engine.config.backend,
            "strategy": engine.scheduler.name,
            "ordering_policy": engine.scheduler.ordering.name,
            "placement_policy": engine.scheduler.placement.name,
            "telemetry_enabled": engine.telemetry_enabled,
            "workers": {w.worker_id: w.status.value for w in fleet.workers},
        }

    # Last, and at the root: every API route above already owns its path, and
    # only unclaimed paths fall through to the SPA. Mounting earlier would
    # shadow the API with a static handler that answers 404 for /healthz.
    _mount_dashboard(app)
    return app


app = create_app()
