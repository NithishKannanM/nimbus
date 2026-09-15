"""Benchmark endpoints -- the Phase 7 exit criterion.

``POST /benchmarks`` must produce an archived ``raw.jsonl`` and a report. The
first test here does exactly that, end to end, with a real engine and real
workloads. The rest cover the surface around it: validation, listing, and the
serialisation guarantee that keeps two runs from measuring each other.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import NimbusConfig, set_config


@pytest.fixture
def client(tmp_path, config):
    set_config(config.model_copy(update={"artifact_dir": str(tmp_path / "artifacts")}))
    with TestClient(create_app()) as c:
        yield c


def wait_for(client: TestClient, benchmark_id: str, timeout_s: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout_s
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/benchmarks/{benchmark_id}").json()
        if body["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return body
        time.sleep(0.25)
    return body


# --------------------------------------------------------------------------- #
# the exit criterion
# --------------------------------------------------------------------------- #
def test_a_submitted_benchmark_archives_raw_jsonl_and_a_report(client: TestClient, tmp_path):
    """POST /benchmarks -> archived raw.jsonl + report. The whole of Phase 7."""
    accepted = client.post(
        "/benchmarks",
        json={
            "profile": "homogeneous_saturated",
            "strategies": ["fifo", "adaptive"],
            "job_count": 12,
            "size_scale": 0.15,
            "warmup_jobs": 0,
            "apply_pressure": False,  # keep the test cheap; pressure is covered elsewhere
            "timeout_s": 120,
        },
    )
    assert accepted.status_code == 202
    benchmark_id = accepted.json()["benchmark_id"]

    final = wait_for(client, benchmark_id)
    assert final["state"] == "COMPLETED", final.get("error")
    assert final["cells_done"] == final["cells_total"] == 2
    assert len(final["run_ids"]) == 2

    # The archive, in the layout docs/architecture.md section 9 specifies.
    for run_id in final["run_ids"]:
        base = Path(tmp_path) / "artifacts" / "benchmarks" / run_id
        for name in ("manifest.json", "metrics.json", "raw.jsonl", "decisions.jsonl"):
            assert (base / name).exists(), f"{run_id} is missing {name}"
        rows = [
            json.loads(line)
            for line in (base / "raw.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert rows, "raw.jsonl is empty"
        assert all("job_id" in row and "status" in row for row in rows)

    # ...and a report derived from it.
    report = client.get(
        "/benchmarks/report", params={"profile": "homogeneous_saturated"}
    ).json()
    assert report["runs_total"] == 2
    assert "# Benchmark report" in report["markdown"]

    # The report must *see* both runs. Whether it admits them as evidence is a
    # separate question, and the answer here is no: twelve tiny jobs on three
    # workers is far below the 50% load-regime gate, and this profile applies no
    # external pressure, so that is genuine low load rather than the gate
    # mis-firing. A smoke test being classed inadmissible is the gate working.
    assert report["runs_admissible"] == 0
    assert len(report["excluded"]) == 2
    assert all("utilisation" in reason for reason in report["excluded"])
    assert set(report["versions_by_strategy"]) == {"fifo", "adaptive"}


def test_the_admissibility_flag_follows_the_measured_utilisation(client: TestClient):
    """The plumbing, not the threshold.

    Whether 0.50 is the right cut-off, and what happens either side of it, is
    settled in tests/unit/test_report.py against synthetic archives -- which is
    where it belongs, because pinning it here would make the assertion depend on
    how loaded the *test machine* happened to be.

    What integration can check, and unit tests cannot, is that the utilisation a
    real run actually measured is the number the flag is computed from. A flag
    wired to the wrong metric, or to a constant, would pass every unit test.
    """
    accepted = client.post(
        "/benchmarks",
        json={
            "profile": "homogeneous_saturated", "strategies": ["fifo"],
            "job_count": 20, "size_scale": 0.3, "warmup_jobs": 0,
            "apply_pressure": False, "timeout_s": 180,
        },
    ).json()
    final = wait_for(client, accepted["benchmark_id"], timeout_s=240)
    assert final["state"] == "COMPLETED", final.get("error")

    run = client.get(f"/benchmarks/runs/{final['run_ids'][0]}").json()
    utilisation = run["metrics"]["mean_fleet_utilisation"]
    assert 0.0 <= utilisation <= 1.0
    assert run["load_regime_ok"] is (utilisation >= 0.50)
    assert run["truncated"] is False

    row = next(
        r for r in client.get("/benchmarks/runs").json()
        if r["run_id"] == final["run_ids"][0]
    )
    assert row["load_regime_ok"] == run["load_regime_ok"], (
        "the listing and the detail view disagree about admissibility"
    )
    assert row["mean_fleet_utilisation"] == pytest.approx(utilisation)


def test_the_archived_manifest_records_what_reproduces_the_run(client: TestClient):
    """A number without a manifest is not a result (docs/experiments.md §2.4)."""
    accepted = client.post(
        "/benchmarks",
        json={
            "profile": "homogeneous_saturated", "strategies": ["fifo"],
            "job_count": 8, "size_scale": 0.1, "warmup_jobs": 0,
            "apply_pressure": False, "timeout_s": 120, "seed": 4242,
        },
    ).json()
    final = wait_for(client, accepted["benchmark_id"])
    assert final["state"] == "COMPLETED", final.get("error")

    run = client.get(f"/benchmarks/runs/{final['run_ids'][0]}").json()
    manifest = run["manifest"]
    assert manifest["seed"] == 4242
    assert manifest["strategy"] == "fifo"
    assert manifest["algorithm_version"]
    assert manifest["job_plan"], "the job plan is what makes the run reproducible"
    assert manifest["host"] and manifest["config"]


def test_an_archived_run_appears_in_the_runs_listing(client: TestClient):
    accepted = client.post(
        "/benchmarks",
        json={
            "profile": "homogeneous_saturated", "strategies": ["fifo"],
            "job_count": 8, "size_scale": 0.1, "warmup_jobs": 0,
            "apply_pressure": False, "timeout_s": 120,
        },
    ).json()
    final = wait_for(client, accepted["benchmark_id"])
    assert final["state"] == "COMPLETED", final.get("error")

    rows = client.get("/benchmarks/runs").json()
    assert final["run_ids"][0] in {row["run_id"] for row in rows}
    row = next(r for r in rows if r["run_id"] == final["run_ids"][0])
    assert row["jobs_completed"] == 8
    assert row["truncated"] is False


# --------------------------------------------------------------------------- #
# profiles
# --------------------------------------------------------------------------- #
def test_the_profile_catalogue_is_exposed(client: TestClient):
    profiles = client.get("/benchmarks/profiles").json()
    names = {p["name"] for p in profiles}
    assert {"homogeneous_saturated", "cpu_pressure", "memory_pressure"} <= names

    pressure_profile = next(p for p in profiles if p["name"] == "cpu_pressure")
    assert pressure_profile["pressure"], "profile B must declare its external pressure"
    assert pressure_profile["pressure"][0]["target_worker"] == "worker-1"
    assert sum(pressure_profile["mix"].values()) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def test_an_unknown_profile_is_422_and_lists_the_real_ones(client: TestClient):
    r = client.post("/benchmarks", json={"profile": "nope", "strategies": ["fifo"]})
    assert r.status_code == 422
    assert "cpu_pressure" in r.json()["detail"]


def test_an_unknown_strategy_is_422(client: TestClient):
    r = client.post(
        "/benchmarks",
        json={"profile": "homogeneous_saturated", "strategies": ["fifo", "priority"]},
    )
    assert r.status_code == 422
    assert "priority" in r.json()["detail"]


def test_an_empty_strategy_list_is_rejected(client: TestClient):
    r = client.post(
        "/benchmarks", json={"profile": "homogeneous_saturated", "strategies": []}
    )
    assert r.status_code == 422


def test_an_unknown_benchmark_is_404(client: TestClient):
    assert client.get("/benchmarks/bench-nope").status_code == 404


def test_an_unknown_run_is_404(client: TestClient):
    assert client.get("/benchmarks/runs/exp-nope").status_code == 404


def test_a_report_for_a_profile_with_no_runs_is_404(client: TestClient):
    r = client.get("/benchmarks/report", params={"profile": "burst"})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def test_a_queued_benchmark_can_be_cancelled(client: TestClient):
    """Two sweeps, cancel the second. Runs are serialised, so the second is
    still QUEUED while the first executes."""
    payload = {
        "profile": "homogeneous_saturated", "strategies": ["fifo"],
        "job_count": 20, "size_scale": 0.3, "warmup_jobs": 0,
        "apply_pressure": False, "timeout_s": 120,
    }
    first = client.post("/benchmarks", json=payload).json()
    second = client.post("/benchmarks", json=payload).json()

    assert client.post(f"/benchmarks/{second['benchmark_id']}/cancel").status_code == 200
    assert client.get(f"/benchmarks/{second['benchmark_id']}").json()["state"] in (
        "CANCELLED",
        "RUNNING",  # it may have started if the first finished very fast
    )
    wait_for(client, first["benchmark_id"])


def test_cancelling_a_finished_benchmark_is_409(client: TestClient):
    accepted = client.post(
        "/benchmarks",
        json={
            "profile": "homogeneous_saturated", "strategies": ["fifo"],
            "job_count": 6, "size_scale": 0.1, "warmup_jobs": 0,
            "apply_pressure": False, "timeout_s": 120,
        },
    ).json()
    wait_for(client, accepted["benchmark_id"])
    assert client.post(f"/benchmarks/{accepted['benchmark_id']}/cancel").status_code == 409


def test_submitted_benchmarks_are_listed_newest_first(client: TestClient):
    payload = {
        "profile": "homogeneous_saturated", "strategies": ["fifo"],
        "job_count": 6, "size_scale": 0.1, "warmup_jobs": 0,
        "apply_pressure": False, "timeout_s": 120,
    }
    first = client.post("/benchmarks", json=payload).json()["benchmark_id"]
    second = client.post("/benchmarks", json=payload).json()["benchmark_id"]

    listed = [b["benchmark_id"] for b in client.get("/benchmarks").json()]
    assert listed.index(second) < listed.index(first)
    wait_for(client, second)


def test_progress_is_reported_while_a_sweep_runs(client: TestClient):
    accepted = client.post(
        "/benchmarks",
        json={
            "profile": "homogeneous_saturated",
            "strategies": ["fifo", "static_resource_aware", "adaptive"],
            "job_count": 8, "size_scale": 0.1, "warmup_jobs": 0,
            "apply_pressure": False, "timeout_s": 120,
        },
    ).json()
    assert accepted["cells_total"] == 3
    assert accepted["progress"] == 0.0

    final = wait_for(client, accepted["benchmark_id"])
    assert final["state"] == "COMPLETED", final.get("error")
    assert final["progress"] == 1.0
