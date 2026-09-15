"""The admin router: gated, destructive, and mounted only on request.

The gate is the test that matters most here. Every route on this router can
destroy running work, and Nimbus has no authentication before Phase 7, so "the
route does not exist" is the only access control there is. A regression that
mounted it by default would hand anyone who can reach the API the ability to
kill the fleet, and nothing else in the system would notice.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import NimbusConfig, set_config


def build_client(config: NimbusConfig, **overrides) -> TestClient:
    cfg = config.model_copy(update=overrides)
    set_config(cfg)
    return TestClient(create_app())


@pytest.fixture
def admin_client(config):
    with build_client(config, enable_admin=True, heartbeat_timeout_s=1.0,
                      heartbeat_interval_s=0.2, reaper_interval_s=0.2) as c:
        yield c


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_admin_routes_do_not_exist_by_default(config):
    """No admin handler may run when the router is not mounted.

    The accepted statuses are 404 *and* 405. When the dashboard has been built,
    the SPA is mounted at `/` and catches unrouted paths; it serves GETs and
    rejects POSTs with 405. Either way no admin code executes, which is the
    property that matters -- and asserting only 404 would make this test pass
    or fail on whether someone had run `npm run build`.
    """
    with build_client(config, enable_admin=False) as client:
        for method, path in (
            ("get", "/admin/reliability"),
            ("post", "/admin/reaper/sweep"),
            ("post", "/admin/workers/worker-1/fail"),
            ("post", "/admin/workers/worker-1/drain"),
            ("get", "/admin/dlq"),
        ):
            response = getattr(client, method)(path)
            assert response.status_code in (404, 405), f"{path} -> {response.status_code}"
            assert "application/json" not in response.headers.get("content-type", "") or (
                "detail" in response.json()
            ), f"{path} produced an admin response"


def test_no_worker_can_be_failed_through_an_unmounted_admin_router(config):
    """The behavioural version of the gate: the fleet is untouched afterwards."""
    with build_client(config, enable_admin=False) as client:
        engine = client.app.state.engine
        client.post("/admin/workers/worker-1/fail")
        assert engine.reliability()["failed_workers"] == []
        assert client.get("/workers/worker-1").json()["status"] == "HEALTHY"


def test_admin_routes_are_absent_from_the_default_schema(config):
    with build_client(config, enable_admin=False) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert not any(p.startswith("/admin") for p in paths)


def test_admin_routes_exist_when_enabled(admin_client: TestClient):
    assert admin_client.get("/admin/reliability").status_code == 200


# --------------------------------------------------------------------------- #
# reliability reporting
# --------------------------------------------------------------------------- #
def test_reliability_reports_every_phase_4_counter(admin_client: TestClient):
    body = admin_client.get("/admin/reliability").json()
    for key in (
        "retried", "timed_out", "worker_lost", "exhausted", "dead_lettered",
        "stale_results", "duplicate_dispatches", "abandoned_executions",
        "dead_letters", "reaper", "backoff", "workers",
    ):
        assert key in body, f"missing reliability counter: {key}"
    assert body["backoff"]["cap_s"] > body["backoff"]["base_s"]
    assert set(body["workers"]) == {"worker-1", "worker-2", "worker-3"}


def test_duplicate_rate_is_published_not_merely_logged(admin_client: TestClient):
    """A system claiming exactly-once completion under at-least-once delivery
    has to show its duplicate rate, or the claim is untested."""
    assert admin_client.get("/admin/reliability").json()["duplicate_dispatches"] == 0


def test_forcing_a_sweep_returns_a_report(admin_client: TestClient):
    body = admin_client.post("/admin/reaper/sweep").json()
    assert set(body) == {
        "swept_at", "timed_out", "worker_lost", "orphaned", "dead_lettered", "total"
    }
    assert body["total"] == 0


def test_the_dlq_is_inspectable(admin_client: TestClient):
    body = admin_client.get("/admin/dlq").json()
    assert body == {"count": 0, "job_ids": [], "jobs": []}


# --------------------------------------------------------------------------- #
# failure simulation
# --------------------------------------------------------------------------- #
def test_failing_and_recovering_a_worker(admin_client: TestClient):
    engine = admin_client.app.state.engine

    failed = admin_client.post("/admin/workers/worker-1/fail").json()
    assert failed["worker_id"] == "worker-1"
    assert failed["mode"] == "crash"
    # Not marked unhealthy by the call: the control plane has to infer it.
    assert admin_client.get("/workers/worker-1").json()["status"] == "HEALTHY"

    assert engine.reliability()["failed_workers"] == ["worker-1"]

    recovered = admin_client.post("/admin/workers/worker-1/recover").json()
    assert recovered["status"] == "HEALTHY"
    assert engine.reliability()["failed_workers"] == []


def test_a_soft_failure_is_labelled_as_a_partition(admin_client: TestClient):
    body = admin_client.post(
        "/admin/workers/worker-2/fail", json={"hard": False}
    ).json()
    assert body["mode"] == "partition"
    assert body["lost_executions"] == []
    admin_client.post("/admin/workers/worker-2/recover")


def test_draining_and_undraining(admin_client: TestClient):
    assert admin_client.post("/admin/workers/worker-3/drain").json()["status"] == "DRAINING"
    assert admin_client.get("/workers/worker-3").json()["status"] == "DRAINING"

    assert (
        admin_client.post("/admin/workers/worker-3/drain", params={"undo": True}).json()["status"]
        == "HEALTHY"
    )
    assert admin_client.get("/workers/worker-3").json()["status"] == "HEALTHY"


@pytest.mark.parametrize("path", ["fail", "recover", "drain"])
def test_unknown_worker_is_404(admin_client: TestClient, path: str):
    assert admin_client.post(f"/admin/workers/worker-99/{path}").status_code == 404
