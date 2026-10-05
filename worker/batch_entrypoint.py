"""AWS Batch entry point for the baseline comparison.

Usage: ``python3 -m worker.batch_entrypoint <job_type> '<payload-json>'``

This is a thin shim over the SAME ``worker.executor.execute`` the Nimbus
worker's pool child runs, against the same closed workload registry. If the
baseline ran different code the comparison would measure two programs, not two
schedulers (docs/aws-batch-baseline.md condition 3).

It prints exactly one JSON record on stdout (CloudWatch Logs picks it up from
the container) and exits 0 on success, 1 if the workload failed, 2 on a usage
error (bad arguments, unknown job type, payload failing validation). Exit 2 is
deterministic, so infra/batch.tf tells Batch not to retry it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from typing import Any, MutableMapping, TextIO

EXIT_OK, EXIT_WORKLOAD_FAILED, EXIT_USAGE = 0, 1, 2
RECORD_SCHEMA = "nimbus.batch.v1"

#: Same knobs as worker.executor._SINGLE_THREAD_ENV (a test pins them equal).
#: Duplicated on purpose: importing worker.executor imports numpy, and these
#: must be set before that happens.
BLAS_ENV = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
)


class UsageError(ValueError):
    pass


def restrict_blas(env: MutableMapping[str, str] = os.environ) -> None:
    """One job, one core -- identical to the Nimbus worker. Must run before numpy loads."""
    for key in BLAS_ENV:
        env.setdefault(key, "1")


def parse_args(argv: list[str]) -> tuple[str, dict]:
    if len(argv) != 2:
        raise UsageError(
            "usage: python -m worker.batch_entrypoint <job_type> '<payload-json>'"
        )
    job_type, raw = argv
    if not job_type:
        raise UsageError("job_type must not be empty")
    if not raw.strip():
        return job_type, {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"payload is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise UsageError(
            f"payload must be a JSON object, got {type(payload).__name__}"
        )
    return job_type, payload


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _record(job_type: str, workload_class: str | None, *, ok: bool,
            result: dict | None, error: str | None, duration_ms: float,
            started: float, finished: float, env: MutableMapping[str, str],
            pid: int) -> dict[str, Any]:
    return {
        "schema": RECORD_SCHEMA,
        "job_type": job_type,
        "workload_class": workload_class,
        "ok": ok,
        "result": result,
        "error": error,
        "duration_ms": duration_ms,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "batch_job_id": env.get("AWS_BATCH_JOB_ID"),
        "batch_attempt": env.get("AWS_BATCH_JOB_ATTEMPT"),
        "pid": pid,
    }


def run(job_type: str, payload: dict, *, env: MutableMapping[str, str] = os.environ,
        clock=time.time) -> tuple[int, dict]:
    """Validate and execute one workload. Returns (exit_code, record)."""
    started = clock()
    pid = os.getpid()

    # Lazy: these import numpy, so restrict_blas() must already have run.
    from pydantic import ValidationError

    from worker.workloads import registry

    try:
        spec, _ = registry.validate(job_type, payload)
    except (registry.UnknownJobType, ValidationError) as exc:
        message = exc.args[0] if isinstance(exc, registry.UnknownJobType) else str(exc)
        finished = clock()
        return EXIT_USAGE, _record(
            job_type, None, ok=False, result=None, error=message,
            duration_ms=(finished - started) * 1000.0, started=started,
            finished=finished, env=env, pid=pid,
        )

    from worker.executor import execute  # same function the pool child runs

    scratch = tempfile.mkdtemp(prefix="nimbus-batch-", dir=env.get("NIMBUS_SCRATCH_DIR"))
    try:
        envelope = execute(job_type, payload, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    finished = clock()

    if not envelope["ok"] and envelope.get("traceback"):
        sys.stderr.write(envelope["traceback"])
    return (EXIT_OK if envelope["ok"] else EXIT_WORKLOAD_FAILED), _record(
        job_type, spec.workload_class.value, ok=envelope["ok"],
        result=envelope["result"], error=envelope["error"],
        duration_ms=envelope["duration_ms"], started=started, finished=finished,
        env=env, pid=envelope.get("pid", pid),
    )


def _emit(record: dict, stdout: TextIO) -> None:
    stdout.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    stdout.flush()


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    restrict_blas()

    try:
        job_type, payload = parse_args(argv)
    except UsageError as exc:
        stderr.write(f"error: {exc}\n")
        now = time.time()
        _emit(_record(argv[0] if argv else "", None, ok=False, result=None,
                      error=str(exc), duration_ms=0.0, started=now, finished=now,
                      env=os.environ, pid=os.getpid()), stdout)
        return EXIT_USAGE

    try:
        code, record = run(job_type, payload)
    except Exception as exc:  # noqa: BLE001 -- never exit without a record
        stderr.write(traceback.format_exc())
        now = time.time()
        _emit(_record(job_type, None, ok=False, result=None,
                      error=f"{type(exc).__name__}: {exc}", duration_ms=0.0,
                      started=now, finished=now, env=os.environ,
                      pid=os.getpid()), stdout)
        return EXIT_WORKLOAD_FAILED

    if code == EXIT_USAGE:
        stderr.write(f"error: {record['error']}\n")
    elif not record["ok"]:
        stderr.write(f"workload failed: {record['error']}\n")
    _emit(record, stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
