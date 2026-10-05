"""Channel 1 observability: operational metrics, published to CloudWatch.

docs/architecture.md section 10 draws a hard line between two channels. This is
the CloudWatch one -- coarse (60 s), cheap, for humans and alarms. It is *not*
the scheduler's telemetry (``WorkerRegistry``), and nothing in the scheduler ever
reads from here.

Two properties shape the design:

**Emission is a dictionary update, never I/O.** ``emit`` is called from the
scheduler tick and the completion path. It folds the value into a per-series
statistic set (count / sum / min / max) under a lock and returns. Network calls
happen only in ``flush``, on a background thread. A thousand completions in a
minute become one datum per series, not a thousand -- which is also what keeps
``PutMetricData`` volume (and the bill, docs/cost-model.md) bounded.

**Metrics never break the thing they measure.** A throttled or failed
``PutMetricData`` is counted and the chunk dropped; ``flush`` does not raise.
Dropping a minute of operational metrics is acceptable. Wedging the control
plane because CloudWatch had a bad minute is not.

boto3 is imported lazily (via ``backend.aws.clients``) and only when no client
is injected, so this module imports on a machine with no AWS dependency.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Protocol

log = logging.getLogger("nimbus.metrics")

#: Pinned: infra/iam.tf conditions ``cloudwatch:PutMetricData`` on this value.
#: Publishing under any other namespace is an AccessDenied, not a typo.
NAMESPACE = "Nimbus"

#: Datums per ``PutMetricData`` call (docs/architecture.md section 10). The API
#: allows 1000; we batch smaller to keep each request well under the payload cap.
MAX_DATUMS_PER_CALL = 20


class Metric(StrEnum):
    JOBS_SUBMITTED = "JobsSubmitted"
    JOBS_COMPLETED = "JobsCompleted"
    JOBS_FAILED = "JobsFailed"
    JOBS_RETRIED = "JobsRetried"
    QUEUE_DEPTH_VISIBLE = "QueueDepthVisible"
    QUEUE_DEPTH_IN_FLIGHT = "QueueDepthInFlight"
    EXECUTION_TIME_MS = "ExecutionTimeMs"
    SCHEDULING_LATENCY_MS = "SchedulingLatencyMs"
    QUEUE_WAIT_MS = "QueueWaitMs"
    WORKER_HEALTHY = "WorkerHealthy"
    DUPLICATE_EXECUTIONS = "DuplicateExecutions"


_COUNT, _MS = "Count", "Milliseconds"

#: CloudWatch unit per metric. ``None`` means omit the field (unitless), which
#: is right for the 0/1 health flag.
UNITS: dict[Metric, str | None] = {
    Metric.JOBS_SUBMITTED: _COUNT,
    Metric.JOBS_COMPLETED: _COUNT,
    Metric.JOBS_FAILED: _COUNT,
    Metric.JOBS_RETRIED: _COUNT,
    Metric.QUEUE_DEPTH_VISIBLE: _COUNT,
    Metric.QUEUE_DEPTH_IN_FLIGHT: _COUNT,
    Metric.EXECUTION_TIME_MS: _MS,
    Metric.SCHEDULING_LATENCY_MS: _MS,
    Metric.QUEUE_WAIT_MS: _MS,
    Metric.WORKER_HEALTHY: None,
    Metric.DUPLICATE_EXECUTIONS: _COUNT,
}

_S, _W, _K = "Strategy", "WorkloadClass", "WorkerId"
_NONE: frozenset[str] = frozenset()

#: Which dimensions each metric may carry. Every dimension combination is a
#: separate billable CloudWatch metric, so the set is closed on purpose: a stray
#: dimension is a ValueError in a unit test, not a surprise on the invoice.
ALLOWED_DIMENSIONS: dict[Metric, frozenset[str]] = {
    Metric.JOBS_SUBMITTED: frozenset({_W}),
    Metric.JOBS_COMPLETED: frozenset({_S, _W}),
    Metric.JOBS_FAILED: frozenset({_S, _W}),
    Metric.JOBS_RETRIED: frozenset({_S, _W}),
    Metric.EXECUTION_TIME_MS: frozenset({_S, _W}),
    Metric.SCHEDULING_LATENCY_MS: frozenset({_S}),
    Metric.QUEUE_WAIT_MS: frozenset({_S, _W}),
    Metric.DUPLICATE_EXECUTIONS: frozenset({_S}),
    Metric.QUEUE_DEPTH_VISIBLE: _NONE,
    Metric.QUEUE_DEPTH_IN_FLIGHT: _NONE,
    Metric.WORKER_HEALTHY: frozenset({_K}),
}

SeriesKey = tuple[Metric, tuple[tuple[str, str], ...]]


@dataclass
class StatisticSet:
    """CloudWatch's ``StatisticValues``: enough to rebuild sum/avg/min/max/count
    for the interval without shipping every sample."""

    count: int
    total: float
    minimum: float
    maximum: float

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        if value < self.minimum:
            self.minimum = value
        if value > self.maximum:
            self.maximum = value


class MetricsSink(Protocol):
    def emit(self, metric: Metric, value: float = 1.0, **dimensions: str) -> None: ...

    def flush(self) -> int:
        """Publish pending data. Returns the number of datums published."""
        ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


def _series_key(metric: Metric, dimensions: dict[str, str]) -> SeriesKey:
    """Validate and normalise. Raises ValueError on anything CloudWatch would
    bill for or reject -- an unknown metric, a dimension the metric may not
    carry, or an empty dimension value."""
    try:
        metric = Metric(metric)
    except ValueError:
        raise ValueError(f"unknown metric: {metric!r}") from None
    if not isinstance(metric, Metric):  # pragma: no cover -- defensive
        raise ValueError(f"unknown metric: {metric!r}")
    extra = set(dimensions) - ALLOWED_DIMENSIONS[metric]
    if extra:
        raise ValueError(
            f"{metric.value} does not take dimension(s) {sorted(extra)}; "
            f"allowed: {sorted(ALLOWED_DIMENSIONS[metric])}"
        )
    items = []
    for name, value in dimensions.items():
        text = str(value)
        if not text:
            raise ValueError(f"dimension {name} for {metric.value} is empty")
        items.append((name, text))
    return metric, tuple(sorted(items))


class _Aggregator:
    """Thread-safe ``{series: StatisticSet}``. ``drain`` swaps the map out, so
    emitters are blocked for a pointer swap, not for a network call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._series: dict[SeriesKey, StatisticSet] = {}

    def add(self, key: SeriesKey, value: float) -> None:
        with self._lock:
            stat = self._series.get(key)
            if stat is None:
                self._series[key] = StatisticSet(1, value, value, value)
            else:
                stat.add(value)

    def drain(self) -> dict[SeriesKey, StatisticSet]:
        with self._lock:
            drained, self._series = self._series, {}
        return drained

    def snapshot(self) -> dict[SeriesKey, StatisticSet]:
        with self._lock:
            return {
                k: StatisticSet(s.count, s.total, s.minimum, s.maximum)
                for k, s in self._series.items()
            }


def _usable(value: float) -> bool:
    """CloudWatch rejects NaN and infinity; one bad sample must not poison the
    whole statistic set (NaN would stick in the sum for the interval)."""
    try:
        return math.isfinite(value)
    except TypeError:
        return False


class NullMetricsSink:
    """Metrics disabled. Still validates names so a typo is caught either way."""

    def emit(self, metric: Metric, value: float = 1.0, **dimensions: str) -> None:
        _series_key(metric, dimensions)

    def flush(self) -> int:
        return 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class InMemoryMetricsSink:
    """The local default: same validation and aggregation as the CloudWatch sink,
    published nowhere. Tests and the local dashboard read it back.

    ``flush`` moves pending series into a bounded history (``published``) so a
    long-running local process cannot grow without limit.
    """

    def __init__(self, *, history: int = 10_000) -> None:
        self._pending = _Aggregator()
        self.published: deque[tuple[SeriesKey, StatisticSet]] = deque(maxlen=history)
        self._lock = threading.Lock()

    def emit(self, metric: Metric, value: float = 1.0, **dimensions: str) -> None:
        key = _series_key(metric, dimensions)
        if _usable(value):
            self._pending.add(key, float(value))

    def flush(self) -> int:
        drained = self._pending.drain()
        with self._lock:
            for key, stat in drained.items():
                self.published.append((key, stat))
        return len(drained)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.flush()

    def _matching(self, metric: Metric, dims: dict[str, str]):
        wanted = {(k, str(v)) for k, v in dims.items()}
        with self._lock:
            published = list(self.published)
        pending = list(self._pending.snapshot().items())
        for (name, series_dims), stat in published + pending:
            if name == metric and wanted <= set(series_dims):
                yield stat

    def total(self, metric: Metric, **dims: str) -> float:
        """Sum of values over every series whose dimensions include ``dims``."""
        return sum(s.total for s in self._matching(metric, dims))

    def count(self, metric: Metric, **dims: str) -> int:
        """Number of samples over every series whose dimensions include ``dims``."""
        return sum(s.count for s in self._matching(metric, dims))


class CloudWatchMetricsSink:
    """Aggregates in memory and publishes via ``PutMetricData`` on a timer."""

    def __init__(
        self,
        *,
        region: str | None = None,
        client: Any = None,
        namespace: str = NAMESPACE,
        flush_interval_s: float = 60.0,
        batch_size: int = MAX_DATUMS_PER_CALL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if client is None:
            from backend.aws.clients import client as make_client  # noqa: PLC0415

            client = make_client("cloudwatch", region=region)
        self._cw = client
        self._namespace = namespace
        self._interval = flush_interval_s
        self._batch_size = batch_size
        self._clock = clock
        self._agg = _Aggregator()
        self._flush_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.calls = 0
        self.failed_calls = 0
        self.dropped_datums = 0

    # -- producer ---------------------------------------------------------
    def emit(self, metric: Metric, value: float = 1.0, **dimensions: str) -> None:
        key = _series_key(metric, dimensions)
        if _usable(value):
            self._agg.add(key, float(value))

    # -- publisher --------------------------------------------------------
    def _datum(self, key: SeriesKey, stat: StatisticSet, when: datetime) -> dict[str, Any]:
        metric, dims = key
        datum: dict[str, Any] = {
            "MetricName": metric.value,
            "Dimensions": [{"Name": n, "Value": v} for n, v in dims],
            "Timestamp": when,
            "StatisticValues": {
                "SampleCount": float(stat.count),
                "Sum": stat.total,
                "Minimum": stat.minimum,
                "Maximum": stat.maximum,
            },
        }
        unit = UNITS[metric]
        if unit is not None:
            datum["Unit"] = unit
        return datum

    def flush(self) -> int:
        """Publish everything pending. Returns datums accepted by CloudWatch.

        Never raises. A failed chunk is counted and dropped rather than retried:
        retrying would hold the next interval's data hostage to this one's
        outage, and the next flush carries fresh aggregates anyway.
        """
        with self._flush_lock:
            drained = self._agg.drain()
            if not drained:
                return 0
            when = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
            datums = [self._datum(k, s, when) for k, s in drained.items()]
            published = 0
            for i in range(0, len(datums), self._batch_size):
                chunk = datums[i : i + self._batch_size]
                self.calls += 1
                try:
                    self._cw.put_metric_data(Namespace=self._namespace, MetricData=chunk)
                except Exception as exc:  # noqa: BLE001 -- metrics must not raise
                    self.failed_calls += 1
                    self.dropped_datums += len(chunk)
                    log.warning("PutMetricData failed, dropped %d datums: %s", len(chunk), exc)
                else:
                    published += len(chunk)
            return published

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="nimbus-metrics-flush", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._state_lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None:
            thread.join(timeout=5.0)
        # Final flush, so a clean shutdown does not lose the last interval.
        self.flush()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.flush()
            except Exception:  # noqa: BLE001 -- keep the loop alive
                log.exception("metrics flush loop error")
