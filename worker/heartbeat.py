"""Worker liveness heartbeats.

Deliberately independent of telemetry. Telemetry ingestion *also* refreshes a
worker's heartbeat -- a worker reporting metrics is self-evidently alive -- but
the converse must not hold: with telemetry disabled, workers still have to
prove liveness, or the Phase 2 telemetry-OFF control arm would watch its entire
fleet drift to UNHEALTHY and place nothing.

A heartbeat carries no resource information. That is the point: liveness is a
correctness signal every strategy needs, while resource observation is the
experimental variable.
"""

from __future__ import annotations

import threading


class HeartbeatEmitter:
    """Emits liveness for every registered local worker on a fixed interval.

    In Phase 5 each EC2 worker emits its own; locally the agents are in-process
    and a single ticker is equivalent.
    """

    def __init__(self, registry, *, interval_s: float = 5.0, is_alive=None) -> None:
        self._registry = registry
        self._interval_s = interval_s
        #: worker_id -> bool. Lets a simulated worker failure stop heartbeating
        #: without being torn down (Phase 4).
        self._is_alive = is_alive or (lambda _worker_id: True)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.beats = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self.beat_once()  # register liveness immediately, don't wait an interval
        self._thread = threading.Thread(
            target=self._run, name="nimbus-heartbeat", daemon=True
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

    def beat_once(self) -> int:
        emitted = 0
        for worker_id in self._registry.worker_ids():
            if self._is_alive(worker_id):
                self._registry.heartbeat(worker_id)
                emitted += 1
        self.beats += emitted
        return emitted

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.beat_once()
            except Exception:
                pass
