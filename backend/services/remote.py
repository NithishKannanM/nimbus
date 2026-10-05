"""HTTP data plane, control-plane side.

``RemoteWorkerClient`` speaks to a worker host (``worker/api.py``) over HTTP and
exposes the same surface the engine uses on ``LocalWorkerAgent``, so the
scheduler, reaper and failure simulation do not know which transport they are
driving. Choosing between them is the factory's job (backend/services/factory.py).

Two failure modes are kept apart on purpose:

* A 409 is "at capacity" -- a fast, normal answer that costs the job nothing.
* Everything else that is not a clean 202 (5xx, connection error, timeout, a
  reply from the wrong worker) is a dispatch error. Both return False so the
  engine's existing rejection path re-queues without spending retry budget.

A read timeout on /execute is ambiguous: the worker may have accepted the job.
The job is then re-dispatched under the same execution_id; the worker's dedupe
cache and the engine's execution-id guard (docs/architecture.md section 8,
guard 3) keep completion exactly-once.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import httpx

from backend.models.job import Job
from backend.models.telemetry import WorkerTelemetry
from worker.telemetry import CollectorStats

log = logging.getLogger("nimbus.remote")

#: connect timeout for dispatch; the read timeout is dispatch_timeout_s.
DISPATCH_CONNECT_TIMEOUT_S = 1.0

_ZERO_STATS: dict[str, int | bool] = {
    "active_jobs": 0,
    "slots": 0,
    "crashed": False,
    "duplicate_dispatches": 0,
    "abandoned_executions": 0,
    "crashes": 0,
}


def parse_worker_endpoints(spec: str) -> dict[str, str]:
    """``"worker-1=http://10.0.0.5:8080,worker-2=..."`` -> ``{id: url}``.

    Order is preserved (it becomes registration order). Raises ValueError on a
    blank spec, an empty id or url, a non-http(s) scheme or a duplicate id.
    """
    if not spec or not spec.strip():
        raise ValueError("worker endpoints are empty")
    endpoints: dict[str, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            raise ValueError("worker endpoints contain an empty entry")
        worker_id, sep, url = item.partition("=")
        worker_id, url = worker_id.strip(), url.strip()
        if not sep or not worker_id:
            raise ValueError(f"worker endpoint {item!r} must look like id=url")
        if not url:
            raise ValueError(f"worker endpoint {worker_id!r} has an empty url")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"worker endpoint {worker_id!r} must be an http(s) url, got {url!r}")
        if worker_id in endpoints:
            raise ValueError(f"duplicate worker id {worker_id!r} in worker endpoints")
        endpoints[worker_id] = url.rstrip("/")
    return endpoints


class RemoteWorkerClient:
    """Same surface the engine uses on LocalWorkerAgent, spoken over HTTP."""

    #: remote hosts are not core-pinned from here; telemetry is host-scoped.
    cores: list[int] | None = None

    def __init__(
        self,
        worker_id: str,
        base_url: str,
        *,
        slots: int,
        reservations: Callable[[str], int],
        dispatch_timeout_s: float = 5.0,
        probe_timeout_s: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.slots = slots
        self._base = base_url.rstrip("/")
        self._reservations = reservations
        self._dispatch_timeout = httpx.Timeout(
            dispatch_timeout_s, connect=min(DISPATCH_CONNECT_TIMEOUT_S, dispatch_timeout_s)
        )
        self._probe_timeout = httpx.Timeout(probe_timeout_s)
        self._transport = transport
        self._lock = threading.Lock()
        self._dispatch: httpx.Client | None = None
        self._probe: httpx.Client | None = None
        self._abandoner: ThreadPoolExecutor | None = None
        self._crashed = False
        self.dispatched = 0
        self.rejected = 0
        self.dispatch_errors = 0
        self.probe_failures = 0
        self.identity_mismatches = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._dispatch is not None:
                return
            # Separate clients: dispatch happens on the scheduler thread and
            # probes on the heartbeat/telemetry threads. A slow probe must not
            # queue behind, or hold up, a dispatch.
            self._dispatch = httpx.Client(
                base_url=self._base, timeout=self._dispatch_timeout, transport=self._transport
            )
            self._probe = httpx.Client(
                base_url=self._base, timeout=self._probe_timeout, transport=self._transport
            )
            self._abandoner = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"nimbus-abandon-{self.worker_id}"
            )

    def stop(self, *, wait: bool = True) -> None:
        with self._lock:
            dispatch, probe, abandoner = self._dispatch, self._probe, self._abandoner
            self._dispatch = self._probe = self._abandoner = None
        if abandoner is not None:
            abandoner.shutdown(wait=wait, cancel_futures=not wait)
        for client in (dispatch, probe):
            if client is not None:
                client.close()

    def _clients(self) -> tuple[httpx.Client, httpx.Client, ThreadPoolExecutor]:
        if self._dispatch is None:
            self.start()
        dispatch, probe, abandoner = self._dispatch, self._probe, self._abandoner
        if dispatch is None or probe is None or abandoner is None:
            raise RuntimeError(f"remote worker {self.worker_id} is stopped")
        return dispatch, probe, abandoner

    # -- dispatch contract -------------------------------------------------
    @property
    def active_jobs(self) -> int:
        """The control plane's own reservation count. A remote /stats call per
        read would put a network round-trip inside the scheduler's hot path."""
        return self._reservations(self.worker_id)

    @property
    def crashed(self) -> bool:
        return self._crashed

    def try_execute(self, job: Job) -> bool:
        dispatch, _, _ = self._clients()
        try:
            response = dispatch.post("/execute", json=job.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            self.dispatch_errors += 1
            log.warning("dispatch of %s to %s failed: %s", job.job_id, self.worker_id, exc)
            return False
        if response.status_code == 409:
            self.rejected += 1
            return False
        if response.status_code != 202:
            self.dispatch_errors += 1
            log.warning("dispatch of %s to %s returned %d", job.job_id, self.worker_id,
                        response.status_code)
            return False
        try:
            body = response.json()
        except ValueError:
            body = None
        reported = body.get("worker_id") if isinstance(body, dict) else None
        if reported != self.worker_id:
            # Wrong host behind this address (stale endpoint list, reused IP).
            # Treating it as accepted would reserve capacity on a worker that
            # will never report the result.
            self.identity_mismatches += 1
            self.dispatch_errors += 1
            log.error("dispatch to %s answered by %r", self.worker_id, reported)
            return False
        self.dispatched += 1
        return True

    def abandon(self, execution_id: str) -> bool:
        """Fire-and-forget. The reaper calls this inside tick(); a dead worker
        must not stall the scheduler for a connect timeout. True means queued."""
        try:
            _, _, abandoner = self._clients()
            abandoner.submit(self._post_abandon, execution_id)
        except RuntimeError:
            return False
        return True

    def _post_abandon(self, execution_id: str) -> None:
        try:
            _, probe, _ = self._clients()
            probe.post("/abandon", json={"execution_id": execution_id})
        except Exception as exc:  # noqa: BLE001
            log.warning("abandon of %s on %s failed: %s", execution_id, self.worker_id, exc)

    # -- probes ------------------------------------------------------------
    def _get(self, path: str) -> Any | None:
        _, probe, _ = self._clients()
        try:
            response = probe.get(path)
            if response.status_code != 200:
                raise httpx.HTTPError(f"{path} returned {response.status_code}")
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.probe_failures += 1
            log.debug("probe %s%s failed: %s", self.worker_id, path, exc)
            return None
        return body

    def alive(self) -> bool:
        body = self._get("/health")
        if not isinstance(body, dict):
            return False
        if body.get("worker_id") != self.worker_id:
            self.identity_mismatches += 1
            return False
        self._crashed = bool(body.get("crashed", False))
        return bool(body.get("ok")) and not self._crashed

    def telemetry(self) -> WorkerTelemetry | None:
        body = self._get("/telemetry")
        if body is None:
            return None
        try:
            sample = WorkerTelemetry.model_validate(body)
        except ValueError:
            self.probe_failures += 1
            return None
        if sample.worker_id != self.worker_id:
            self.identity_mismatches += 1
            return None
        return sample

    def stats(self) -> dict[str, int | bool]:
        body = self._get("/stats")
        merged = dict(_ZERO_STATS)
        merged["slots"] = self.slots
        merged["active_jobs"] = self.active_jobs
        if isinstance(body, dict):
            merged.update(body)
            merged["reachable"] = True
        else:
            merged["reachable"] = False
        return merged

    # -- failure simulation ------------------------------------------------
    def crash(self) -> tuple[str, ...]:
        raise NotImplementedError("hard-fail a remote worker on its host")

    def recover(self) -> None:
        """No-op: recovery is the host's business. The engine clears its own
        failed-worker mark, and the next heartbeat probe does the rest."""

    def child_pids(self) -> tuple[int, ...]:
        return ()


class RemoteTelemetryCollector:
    """Drop-in for ``worker.telemetry.TelemetryCollector`` that polls each
    remote worker's /telemetry instead of sampling local psutil.

    Workers simulated as failed are skipped: ``registry.ingest_telemetry`` also
    refreshes the heartbeat, so polling a partitioned worker would quietly
    undo the partition.
    """

    def __init__(
        self,
        registry,
        *,
        workers: Callable[[], Mapping[str, RemoteWorkerClient]],
        interval_s: float,
        is_alive: Callable[[str], bool],
    ) -> None:
        self._registry = registry
        self._workers = workers
        self._interval_s = interval_s
        self._is_alive = is_alive
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.stats = CollectorStats()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="nimbus-telemetry-remote", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.collect_once()
            except Exception:
                self.stats.errors += 1

    def collect_once(self) -> list[WorkerTelemetry]:
        tick_started = time.perf_counter()
        samples: list[WorkerTelemetry] = []
        for worker_id, client in list(self._workers().items()):
            if not self._is_alive(worker_id):
                continue
            sample_started = time.perf_counter()
            sample = client.telemetry()
            if sample is None:
                self.stats.errors += 1
                continue
            self._registry.ingest_telemetry(sample)
            samples.append(sample)
            self.stats.samples += 1
            self.stats.total_sample_ms += (time.perf_counter() - sample_started) * 1000.0
        self.stats.ticks += 1
        self.stats.total_tick_ms += (time.perf_counter() - tick_started) * 1000.0
        return samples
