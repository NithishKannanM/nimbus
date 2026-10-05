"""AWS Batch entry point: same executor, one JSON record, distinct exit codes."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from worker import batch_entrypoint as be

REPO = Path(__file__).resolve().parents[2]


def _run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    code = be.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _record(stdout: str) -> dict:
    lines = stdout.splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


# -- argument parsing ------------------------------------------------------

def test_parse_args_ok():
    assert be.parse_args(["prime_count", '{"limit": 5000}']) == ("prime_count", {"limit": 5000})


def test_parse_args_empty_payload_is_empty_dict():
    assert be.parse_args(["json_transform", ""]) == ("json_transform", {})


@pytest.mark.parametrize("argv", [[], ["prime_count"], ["a", "{}", "extra"]])
def test_parse_args_wrong_count(argv):
    with pytest.raises(be.UsageError):
        be.parse_args(argv)


def test_parse_args_invalid_json():
    with pytest.raises(be.UsageError, match="not valid JSON"):
        be.parse_args(["prime_count", "{nope"])


def test_parse_args_json_list_rejected():
    with pytest.raises(be.UsageError, match="JSON object"):
        be.parse_args(["prime_count", "[1, 2]"])


# -- BLAS restriction ------------------------------------------------------

def test_restrict_blas_sets_all_and_respects_override():
    env = {"OMP_NUM_THREADS": "4"}
    be.restrict_blas(env)
    assert env["OMP_NUM_THREADS"] == "4"
    for key in be.BLAS_ENV:
        assert key in env
    assert all(env[k] == "1" for k in be.BLAS_ENV if k != "OMP_NUM_THREADS")


def test_blas_env_matches_worker_executor():
    from worker import executor
    assert be.BLAS_ENV == executor._SINGLE_THREAD_ENV


# -- main ------------------------------------------------------------------

def test_main_runs_prime_count(monkeypatch):
    monkeypatch.setenv("AWS_BATCH_JOB_ID", "job-123")
    monkeypatch.setenv("AWS_BATCH_JOB_ATTEMPT", "2")
    code, out, _ = _run_main(["prime_count", '{"limit":100000}'])
    assert code == be.EXIT_OK
    rec = _record(out)
    assert rec["schema"] == be.RECORD_SCHEMA
    assert rec["ok"] is True and rec["error"] is None
    assert rec["result"]["primes"] == 9592
    assert rec["workload_class"] == "CPU_BOUND"
    assert rec["batch_job_id"] == "job-123"
    assert rec["batch_attempt"] == "2"
    assert rec["started_at"] <= rec["finished_at"]


def test_main_unknown_job_type_lists_known_types():
    code, out, err = _run_main(["not_a_workload", "{}"])
    assert code == be.EXIT_USAGE
    assert "prime_count" in err and "json_transform" in err
    assert _record(out)["ok"] is False


def test_main_invalid_payload_is_usage_error():
    code, out, _ = _run_main(["prime_count", '{"limit": -1}'])
    assert code == be.EXIT_USAGE
    assert _record(out)["ok"] is False


def test_main_bad_args_is_usage_error():
    code, out, err = _run_main(["prime_count"])
    assert code == be.EXIT_USAGE
    assert "usage" in err
    assert _record(out)["ok"] is False


def test_main_runtime_failure_exits_1(monkeypatch):
    from worker import executor

    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(executor, "run_workload", boom)
    code, out, err = _run_main(["prime_count", '{"limit":5000}'])
    assert code == be.EXIT_WORKLOAD_FAILED
    rec = _record(out)
    assert rec["ok"] is False
    assert "RuntimeError: kaboom" in rec["error"]
    assert "kaboom" in err


def test_main_uses_shared_executor_function(monkeypatch):
    from worker import executor

    calls = []
    real = executor.execute

    def spy(job_type, payload, scratch=None):
        calls.append((job_type, payload))
        return real(job_type, payload, scratch)

    monkeypatch.setattr(executor, "execute", spy)
    code, _, _ = _run_main(["prime_count", '{"limit":5000}'])
    assert code == be.EXIT_OK
    assert calls == [("prime_count", {"limit": 5000})]


def test_scratch_dir_cleaned_up(monkeypatch, tmp_path):
    monkeypatch.setenv("NIMBUS_SCRATCH_DIR", str(tmp_path))
    code, _, _ = _run_main(["compress_file", '{"mb":2}'])
    assert code == be.EXIT_OK
    assert list(tmp_path.iterdir()) == []


# -- wiring ----------------------------------------------------------------

def test_module_is_importable_by_batch_command():
    assert importlib.util.find_spec("worker.batch_entrypoint") is not None


def test_terraform_command_matches_entrypoint():
    tf = (REPO / "infra" / "batch.tf").read_text(encoding="utf-8")
    assert re.search(
        r'command\s*=\s*\["python3",\s*"-m",\s*"worker\.batch_entrypoint",'
        r'\s*"Ref::job_type",\s*"Ref::payload"\]', tf)
    # usage errors must not be retried
    assert re.search(r'action\s*=\s*"EXIT"\s*\n\s*on_exit_code\s*=\s*"2"', tf)


def test_subprocess_smoke():
    proc = subprocess.run(
        [sys.executable, "-m", "worker.batch_entrypoint", "json_transform", '{"records":500}'],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    rec = _record(proc.stdout)
    assert rec["ok"] is True and rec["workload_class"] == "LIGHTWEIGHT"
