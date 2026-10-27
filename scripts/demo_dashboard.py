#!/usr/bin/env python
"""Live-seed the running dashboard.

Unlike demo_phase1/2/3, this script does not own an engine. The dashboard
reads from the ONE NimbusEngine that backend/api/app.py's lifespan starts
under uvicorn; building a second engine in-process here would create a
second, invisible fleet fighting the real one for cores, and the dashboard
would never see it. So this talks to the already-running API over HTTP,
exactly the way the dashboard itself does, and applies external pressure
directly -- pressure is an OS process, not an engine concern, so it needs no
engine to start.

Run alongside the server, not instead of it:

    uvicorn backend.api.app:app          # terminal 1
    python scripts/demo_dashboard.py     # terminal 2
    npm --prefix frontend run dev        # terminal 3 (or open the built dist/)

Ctrl+C stops the job stream and the external stressor. It does not stop the
server.

Against the AWS deployment, the stressor has to run on worker-1's own EC2 host,
not on this laptop. ``--ssm-pressure`` does that through SSM Run Command: a
plain CPU hog on the worker host that Nimbus never placed and holds no
reservation for, which is exactly the "noisy neighbour" the experiment is
about. scripts/demo/simulate.sh wires it up from the Terraform outputs.

Nothing here calls the benchmark runner or writes into
.nimbus/artifacts/benchmarks/ -- that archive is the project's real
experimental evidence (docs/results.md is generated from it), and a demo run
landing in it would be indistinguishable from a genuine measurement. Unlike
demo_phase2.py/demo_phase3.py, which rely on remembering to redirect
artifact_dir to a separate DEMO_ARTIFACT_DIR, this script simply never
imports the archiving path at all: it only calls POST /jobs (an ordinary
submission, identical in the store to any other job) and starts/stops an OS
process. There is no code path here that can reach the archive.

Job sizing is deliberate, not "somewhere in the middle of the allowed range".
Three of the nine job types are vectorised (matrix_multiply via BLAS,
prime_count via C-level bytearray slicing, sort_large via numpy) and finish in
tens of milliseconds regardless of parameter -- fine for variety in the jobs
log, useless as a load driver. hash_chain is a pure-Python interpreter loop
over SHA-256 and is the one workload whose duration scales predictably with
its parameter, so it carries the CPU-pressure story: rounds sampled from
4-8 million land around 1-2 seconds each on one core, long enough to be
visibly running rather than instantaneous.
"""

from __future__ import annotations

import argparse
import atexit
import os
import random
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from benchmark.pressure import ExternalPressure, start_cpu_pressure

# Light, fast job types included purely for variety in the jobs log and to
# exercise every workload class -- none of these is expected to take more
# than a few hundred milliseconds, so they add throughput and visual variety
# without competing with hash_chain as the load driver.
_LIGHT_PAYLOADS: list[tuple[str, dict]] = [
    ("prime_count", {"limit": 300_000}),
    ("matrix_multiply", {"size": 350}),
    ("sort_large", {"elements": 400_000}),
    ("json_transform", {"records": 2_000}),
    ("metadata_extract", {"count": 100}),
    ("compress_file", {"mb": 4, "level": 4}),
    ("file_scan", {"mb": 8, "chunk_kb": 64}),
]

#: hash_chain rounds sampled uniformly from this range: ~1-2s per job on one
#: core, based on a measured 20,000,000 rounds -> 4.69s (roughly 4.3M
#: rounds/sec). Long enough to be visibly running, short enough not to starve
#: the queue.
_HASH_CHAIN_ROUNDS = (4_000_000, 8_000_000)

#: Weight given to hash_chain in the submission mix. Biased heavily toward it
#: because it is the only workload here that reliably keeps a slot occupied
#: long enough for the divergence strip and decision ledger to have something
#: to show.
_HASH_CHAIN_WEIGHT = 0.6


def _pick_job(rng: random.Random) -> tuple[str, dict]:
    if rng.random() < _HASH_CHAIN_WEIGHT:
        rounds = rng.randint(*_HASH_CHAIN_ROUNDS)
        return "hash_chain", {"rounds": rounds}
    return rng.choice(_LIGHT_PAYLOADS)


def _cores_for(worker_index: int, worker_count: int) -> tuple[int, ...]:
    """Mirrors NimbusEngine._cores_for exactly; must move together if that
    changes. Duplicated rather than imported because importing
    backend.services.engine here would pull in the whole engine module tree
    for one static computation, and this only needs to be correct on the same
    host uvicorn is running on -- which is the documented case for this
    script."""
    try:
        available = sorted(os.sched_getaffinity(0))
    except AttributeError:
        return ()
    per_worker = max(1, len(available) // max(1, worker_count))
    chunk = available[worker_index * per_worker : (worker_index + 1) * per_worker]
    return tuple(chunk)


class SsmCpuPressure:
    """External CPU pressure on a worker's EC2 host, via SSM Run Command.

    Same interface as benchmark.pressure.ExternalPressure (describe, stop), so
    the demo loop does not care where the pressure runs. One busy loop per
    vCPU, so a t3.small worker reads ~100%. Every loop is wrapped in
    ``timeout``, so the host stops it by itself even if this script dies
    without cleaning up; stop() ends it early.

    Cost note: the t3 instances run in the default "unlimited" credit mode, so
    a pinned worker accrues surplus-credit charges (roughly $0.08/hour for a
    t3.small) on top of the instance price while the pressure runs.
    """

    MARK = "nimbus-demo-pressure"

    def __init__(self, worker_id: str, *, max_seconds: int, region: str | None = None) -> None:
        import boto3  # noqa: PLC0415  (AWS-only dependency, requirements-aws.txt)

        session = boto3.Session(region_name=region)
        self.region = session.region_name
        self._ssm = session.client("ssm")
        self.worker_id = worker_id
        self.max_seconds = max_seconds
        self._stopped = False

        reservations = session.client("ec2").describe_instances(
            Filters=[
                {"Name": "tag:Project", "Values": ["nimbus"]},
                {"Name": "tag:WorkerId", "Values": [worker_id]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        )["Reservations"]
        instances = [i["InstanceId"] for r in reservations for i in r["Instances"]]
        if len(instances) != 1:
            raise SystemExit(
                f"expected one running instance tagged WorkerId={worker_id} in "
                f"{self.region}, found {instances or 'none'}. Is the stack deployed, "
                "and is AWS_PROFILE pointing at that account?"
            )
        self.instance_id = instances[0]
        self._run(
            f"pkill -f {self.MARK} || true",
            f"for i in $(seq $(nproc)); do setsid nohup timeout {max_seconds} "
            f"bash -c ': {self.MARK}; while :; do :; done' >/dev/null 2>&1 & done",
            "sleep 1",
            f"echo running $(pgrep -f 'bash -c : {self.MARK}' | wc -l) busy loops",
        )

    def _run(self, *commands: str) -> str:
        command_id = self._ssm.send_command(
            InstanceIds=[self.instance_id],
            DocumentName="AWS-RunShellScript",
            Comment="nimbus demo: external CPU pressure",
            Parameters={"commands": list(commands)},
        )["Command"]["CommandId"]
        for _ in range(30):
            time.sleep(1.0)
            try:
                result = self._ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=self.instance_id
                )
            except self._ssm.exceptions.InvocationDoesNotExist:
                continue
            if result["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
                if result["Status"] != "Success":
                    raise RuntimeError(f"SSM command {result['Status']}: {result['StandardErrorContent']}")
                return result["StandardOutputContent"].strip()
        raise RuntimeError("SSM command did not finish within 30s")

    def describe(self) -> str:
        return (
            f"CPU hog on {self.worker_id}'s EC2 host ({self.instance_id}, {self.region}) "
            f"via SSM, every vCPU, auto-stops after {self.max_seconds}s"
        )

    def stop(self, timeout: float = 3.0) -> None:  # noqa: ARG002  (interface parity)
        if self._stopped:
            return
        self._stopped = True
        try:
            self._run(f"pkill -f {self.MARK} || true")
            print(f"stopped the CPU hog on {self.worker_id} ({self.instance_id})")
        except Exception as exc:  # noqa: BLE001  (best effort; the host timeout still applies)
            print(f"could not stop the CPU hog over SSM ({exc}); it ends by itself within {self.max_seconds}s")


def _wait_for_api(client: httpx.Client, retries: int = 30) -> None:
    for attempt in range(retries):
        try:
            response = client.get("/healthz", timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if attempt == 0:
            print("waiting for the API to come up...")
        time.sleep(1.0)
    raise SystemExit(
        "could not reach the API. Start it first:\n"
        "    uvicorn backend.api.app:app"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--target-worker", default="worker-1")
    parser.add_argument("--intensity", type=float, default=1.0)
    parser.add_argument("--rate", type=float, default=1.2, help="jobs submitted per second")
    parser.add_argument("--duration", type=float, default=None, help="seconds; omit to run until Ctrl+C")
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument(
        "--ssm-pressure",
        action="store_true",
        help="apply the CPU pressure on the target worker's EC2 host via SSM (AWS deployment)",
    )
    parser.add_argument("--aws-region", default=None, help="defaults to the AWS profile's region")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    client = httpx.Client(base_url=args.api, timeout=10.0)
    _wait_for_api(client)

    workers = client.get("/workers").json()
    worker_ids = sorted(w["worker_id"] for w in workers)
    if args.target_worker not in worker_ids:
        raise SystemExit(f"no such worker {args.target_worker!r}; have {worker_ids}")

    slots = max((w["declared"]["slots"] for w in workers), default=2)
    worker_count = len(worker_ids)

    stressor: ExternalPressure | SsmCpuPressure
    if args.ssm_pressure:
        # The host-side timeout outlives a fixed-length run by a minute; an
        # open-ended run is capped at an hour and can simply be restarted.
        cap = int(args.duration) + 60 if args.duration else 3600
        stressor = SsmCpuPressure(args.target_worker, max_seconds=cap, region=args.aws_region)
    else:
        target_index = worker_ids.index(args.target_worker)
        cores = _cores_for(target_index, worker_count)
        stressor = start_cpu_pressure(args.target_worker, cores, intensity=args.intensity)
    print(f"pressure: {stressor.describe()}")

    def _stop(*_args: object) -> None:
        stressor.stop()
        print("\nstopped external pressure; server keeps running")
        sys.exit(0)

    atexit.register(stressor.stop)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    interval = 1.0 / args.rate
    backlog_ceiling = worker_count * slots * 3
    submitted = 0
    started = time.monotonic()
    last_status = 0.0

    print(
        f"submitting jobs at ~{args.rate}/s "
        f"(hash_chain weight {_HASH_CHAIN_WEIGHT:.0%}, rounds {_HASH_CHAIN_ROUNDS})"
    )
    print("open the dashboard now; Ctrl+C here stops the demo\n")

    while args.duration is None or time.monotonic() - started < args.duration:
        job_type, payload = _pick_job(rng)
        try:
            client.post("/jobs", json={"job_type": job_type, "payload": payload})
            submitted += 1
        except httpx.HTTPError as exc:
            print(f"submit failed: {exc}")

        # Status reads are best effort. Over the internet to an AWS deployment a
        # single slow response is normal, and it must not end a long demo; a
        # dead API shows up as repeated "unreachable" lines instead.
        try:
            if submitted % 10 == 0:
                state = client.get("/scheduler").json()
                backlog = state["queue_visible"] + state["queue_in_flight"]
                if backlog > backlog_ceiling:
                    time.sleep(2.0)

            now = time.monotonic()
            if now - last_status > 4.0:
                last_status = now
                state = client.get("/scheduler").json()
                target = next(
                    (w for w in client.get("/workers").json() if w["worker_id"] == args.target_worker),
                    None,
                )
                observed = (target or {}).get("observed") or {}
                cpu = observed.get("cpu_percent")
                print(
                    f"submitted={submitted:4d}  "
                    f"queue={state['queue_visible']}+{state['queue_in_flight']}  "
                    f"completed={state['completed']}  "
                    f"{args.target_worker} observed_cpu="
                    f"{'n/a' if cpu is None else f'{cpu:.0f}%'}",
                    flush=True,
                )
        except httpx.HTTPError as exc:
            print(f"API unreachable ({type(exc).__name__}); still trying", flush=True)
            time.sleep(5.0)

        time.sleep(interval)

    _stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
