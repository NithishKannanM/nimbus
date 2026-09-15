"""Data-plane execution invariants that the benchmark's validity depends on."""

from __future__ import annotations

import os

from worker.agent import LocalWorkerAgent
from worker.executor import _SINGLE_THREAD_ENV, execute, force_single_threaded_math


def test_force_single_threaded_math_sets_every_backend(monkeypatch):
    for key in _SINGLE_THREAD_ENV:
        monkeypatch.delenv(key, raising=False)
    force_single_threaded_math()
    assert {os.environ[k] for k in _SINGLE_THREAD_ENV} == {"1"}


def test_an_explicit_operator_override_is_respected(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    force_single_threaded_math()
    assert os.environ["OMP_NUM_THREADS"] == "4"


def test_agent_start_restricts_blas_before_spawning_children(monkeypatch, tmp_path):
    """One job must mean one core.

    numpy's matmul is multi-threaded by default: unrestricted, a single
    matrix_multiply job spreads over every core in its worker's affinity set,
    which makes slots and declared cpu_units fiction and would invalidate every
    load-balance measurement.
    """
    for key in _SINGLE_THREAD_ENV:
        monkeypatch.delenv(key, raising=False)
    agent = LocalWorkerAgent("worker-x", slots=1, sink=lambda _r: None, scratch_dir=str(tmp_path))
    agent.start()
    try:
        assert {os.environ[k] for k in _SINGLE_THREAD_ENV} == {"1"}
    finally:
        agent.stop(wait=False)


def test_execute_captures_failure_as_data_not_an_exception():
    envelope = execute("prime_count", {"limit": -1})
    assert envelope["ok"] is False
    assert "ValidationError" in envelope["error"]
    assert "traceback" in envelope
    assert envelope["duration_ms"] >= 0


def test_execute_returns_the_result_envelope():
    envelope = execute("prime_count", {"limit": 1_000})
    assert envelope["ok"] is True
    assert envelope["result"]["primes"] == 168
    assert envelope["pid"] == os.getpid()
