"""API surface. Exercised against a live engine through the real lifespan."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from tests.conftest import SMALL_PAYLOADS


@pytest.fixture
def client(config):
    with TestClient(create_app()) as c:
        yield c


def test_healthz_reports_three_healthy_workers(client: TestClient):
    body = client.get("/healthz").json()
    assert body["ok"] is True and body["phase"] == 7
    assert body["strategy"] == "fifo"
    assert body["ordering_policy"] == "fifo"
    assert body["placement_policy"] == "round_robin"
    assert set(body["workers"]) == {"worker-1", "worker-2", "worker-3"}
    assert set(body["workers"].values()) == {"HEALTHY"}


def test_submit_and_poll_a_job_to_completion(client: TestClient):
    created = client.post(
        "/jobs", json={"job_type": "prime_count", "payload": {"limit": 100_000}, "priority": 2}
    )
    assert created.status_code == 201
    job = created.json()
    assert job["status"] == "QUEUED"
    assert job["workload_class"] == "CPU_BOUND"
    assert job["cpu_units"] > 0 and job["memory_mb"] > 0

    client.app.state.engine.wait_for_idle(timeout_s=60)

    done = client.get(f"/jobs/{job['job_id']}").json()
    assert done["status"] == "COMPLETED"
    assert done["result"]["primes"] == 9592
    assert done["assigned_worker"]


def test_batch_submission_and_listing(client: TestClient):
    payload = {
        "jobs": [
            {"job_type": t, "payload": SMALL_PAYLOADS[t]}
            for t in ("json_transform", "hash_chain", "file_scan")
        ]
    }
    assert client.post("/jobs/batch", json=payload).status_code == 201
    client.app.state.engine.wait_for_idle(timeout_s=60)

    completed = client.get("/jobs", params={"status": "COMPLETED"}).json()
    assert len(completed) == 3
    assert client.get("/jobs", params={"status": "FAILED"}).json() == []


def test_unknown_job_type_is_404(client: TestClient):
    r = client.post("/jobs", json={"job_type": "mine_bitcoin", "payload": {}})
    assert r.status_code == 404
    assert "unknown job_type" in r.json()["detail"]


def test_out_of_bounds_payload_is_422(client: TestClient):
    r = client.post("/jobs", json={"job_type": "sort_large", "payload": {"elements": 10**12}})
    assert r.status_code == 422


def test_unknown_job_is_404(client: TestClient):
    assert client.get("/jobs/job-does-not-exist").status_code == 404


def test_workers_endpoints(client: TestClient):
    workers = client.get("/workers").json()
    assert [w["worker_id"] for w in workers] == ["worker-1", "worker-2", "worker-3"]
    # DECLARED / RESERVED / OBSERVED are separate blocks, never flattened.
    assert all(w["declared"]["slots"] == 2 for w in workers)
    assert all(w["reserved"]["active_jobs"] == 0 for w in workers)
    assert all("observed" in w for w in workers)

    one = client.get("/workers/worker-2").json()
    assert one["worker_id"] == "worker-2" and one["status"] == "HEALTHY"
    assert client.get("/workers/worker-99").status_code == 404


def test_scheduler_endpoint_reports_state(client: TestClient):
    body = client.get("/scheduler").json()
    assert body["strategy"] == "fifo"
    assert "fifo" in body["available"]
    assert "status_counts" in body and "queue_visible" in body


def test_adaptive_is_selectable_now_that_it_exists(client: TestClient):
    """Phase 3 flipped this: `adaptive` was a 422 until adaptive-v1 shipped.

    Partially implemented functionality must never be exposed, so this test
    tracks the real contract rather than being relaxed to accommodate it.
    """
    r = client.put("/scheduler", json={"strategy": "adaptive"})
    assert r.status_code == 200
    body = r.json()
    assert body["strategy"] == "adaptive"
    assert body["ordering_policy"] == "fifo", "ordering must stay constant across strategies"
    assert body["placement_policy"] == "adaptive"

    status = client.get("/scheduler").json()
    assert status["strategy"] == "adaptive"
    assert status["algorithm_version"] == "adaptive-v1"


def test_adaptive_actually_places_jobs(client: TestClient):
    client.put("/scheduler", json={"strategy": "adaptive"})
    created = client.post(
        "/jobs", json={"job_type": "prime_count", "payload": {"limit": 200_000}}
    ).json()
    client.app.state.engine.wait_for_idle(timeout_s=60)

    done = client.get(f"/jobs/{created['job_id']}").json()
    assert done["status"] == "COMPLETED"

    decision = client.get(
        "/scheduler/decisions", params={"job_id": created["job_id"]}
    ).json()[0]
    assert decision["placement_policy"] == "adaptive"
    assert decision["algorithm_version"] == "adaptive-v1"
    assert set(decision["weights"]) == {"cpu", "memory", "reservation", "queue", "trend"}


def test_still_unimplemented_strategies_are_rejected(client: TestClient):
    for name in ("priority", "adaptive-v2", "ml"):
        r = client.put("/scheduler", json={"strategy": name})
        assert r.status_code == 422, name
        assert "unknown strategy" in r.json()["detail"]
    assert client.get("/scheduler").json()["strategy"] == "fifo"


def test_decisions_are_exposed_and_explainable(client: TestClient):
    job = client.post("/jobs", json={"job_type": "json_transform", "payload": {"records": 200}}).json()
    client.app.state.engine.wait_for_idle(timeout_s=60)

    decisions = client.get("/scheduler/decisions", params={"job_id": job["job_id"]}).json()
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["strategy"] == "fifo"
    assert decision["selected_worker"] in {"worker-1", "worker-2", "worker-3"}
    assert decision["candidates"]
    assert decision["reason"]
    assert decision["decision_latency_us"] > 0


def test_comparison_attributes_jobs_to_the_strategy_that_placed_them(client: TestClient):
    def run_one() -> None:
        client.post("/jobs", json={"job_type": "json_transform", "payload": {"records": 200}})
        client.app.state.engine.wait_for_idle(timeout_s=60)

    run_one()
    client.put("/scheduler", json={"strategy": "adaptive"})
    run_one()
    run_one()

    body = client.get("/scheduler/comparison").json()
    assert body["active"] == "adaptive"
    rows = {row["strategy"]: row for row in body["strategies"]}
    assert list(rows) == ["fifo", "adaptive"]
    assert (rows["fifo"]["placed"], rows["fifo"]["completed"]) == (1, 1)
    assert (rows["adaptive"]["placed"], rows["adaptive"]["completed"]) == (2, 2)
    assert rows["adaptive"]["reads_telemetry"] and not rows["fifo"]["reads_telemetry"]
    assert rows["fifo"]["mean_exec_s"] > 0
    assert rows["fifo"]["active_s"] >= 0


def test_workloads_catalogue_lists_every_type(client: TestClient):
    catalogue = client.get("/workloads").json()
    assert {w["job_type"] for w in catalogue} == set(SMALL_PAYLOADS)
    assert {w["workload_class"] for w in catalogue} == {
        "CPU_BOUND", "MEMORY_BOUND", "IO_BOUND", "LIGHTWEIGHT"
    }
    matrix = next(w for w in catalogue if w["job_type"] == "matrix_multiply")
    assert "size" in matrix["parameters"]


# --------------------------------------------------------------------------- #
# dashboard mount (Phase 6)
# --------------------------------------------------------------------------- #
def test_the_dashboard_mount_never_shadows_the_api(client: TestClient):
    """The SPA is mounted at `/`, so ordering matters.

    Mounted before the routers, a catch-all static handler answers 404 for
    /healthz and every other API path -- and the failure looks like the API
    being broken rather than like a mount-order mistake. Every route below must
    keep working whether or not `frontend/dist` exists.
    """
    for path in ("/healthz", "/workers", "/scheduler", "/jobs", "/workloads",
                 "/benchmarks/profiles", "/openapi.json"):
        assert client.get(path).status_code == 200, path


def test_the_api_starts_without_a_built_frontend(client: TestClient):
    """The dashboard is a separate build with its own toolchain. Every test in
    this repo, and the whole benchmark harness, runs with no frontend built."""
    from pathlib import Path

    import backend.api.app as app_module

    dist = Path(app_module.__file__).resolve().parents[2] / "frontend" / "dist"
    root = client.get("/")
    if (dist / "index.html").exists():
        assert root.status_code == 200
        assert "text/html" in root.headers["content-type"]
    else:
        assert root.status_code == 404
