"""Channel 1 metrics: aggregation, batching and failure behaviour.

boto3 is never imported; the sink takes an injected client, and ``FakeCloudWatch``
enforces the request-shape rules a real ``PutMetricData`` would.
"""

from __future__ import annotations

import ast
import re
import time
from pathlib import Path

import pytest

from backend.services import metrics as metrics_module
from backend.services.metrics import (
    ALLOWED_DIMENSIONS,
    MAX_DATUMS_PER_CALL,
    NAMESPACE,
    UNITS,
    CloudWatchMetricsSink,
    InMemoryMetricsSink,
    Metric,
    NullMetricsSink,
)
from tests.unit.fakes_aws import FakeCloudWatch

REPO = Path(__file__).resolve().parents[2]


def sink(cw: FakeCloudWatch | None = None, **kwargs) -> tuple[CloudWatchMetricsSink, FakeCloudWatch]:
    cw = cw or FakeCloudWatch()
    kwargs.setdefault("flush_interval_s", 3600.0)
    return CloudWatchMetricsSink(client=cw, clock=lambda: 1_700_000_000.0, **kwargs), cw


def test_metric_names_match_the_architecture_doc():
    text = (REPO / "docs" / "architecture.md").read_text(encoding="utf-8")
    section = text.split("## 10. Observability", 1)[1].split("## 11.", 1)[0]
    values = [m.value for m in Metric]
    assert len(values) == 11
    for name in values:
        assert name in section
    assert set(UNITS) == set(Metric) == set(ALLOWED_DIMENSIONS)


def test_namespace_is_the_one_iam_allows():
    assert NAMESPACE == "Nimbus"
    iam = (REPO / "infra" / "iam.tf").read_text(encoding="utf-8")
    assert re.search(r'"cloudwatch:namespace"\s*=\s*"Nimbus"', iam)


def test_repeated_emits_collapse_into_one_statistic_set():
    s, cw = sink()
    for v in (10.0, 30.0, 20.0):
        s.emit(Metric.EXECUTION_TIME_MS, v, Strategy="fifo", WorkloadClass="CPU_BOUND")
    assert s.flush() == 1
    (datum,) = cw.datums("ExecutionTimeMs")
    assert datum["StatisticValues"] == {
        "SampleCount": 3.0, "Sum": 60.0, "Minimum": 10.0, "Maximum": 30.0,
    }
    assert datum["Unit"] == "Milliseconds"
    assert cw.calls[0]["Namespace"] == "Nimbus"


def test_dimension_order_is_irrelevant_and_distinct_dims_are_separate():
    s, cw = sink()
    s.emit(Metric.JOBS_COMPLETED, Strategy="fifo", WorkloadClass="CPU_BOUND")
    s.emit(Metric.JOBS_COMPLETED, WorkloadClass="CPU_BOUND", Strategy="fifo")
    s.emit(Metric.JOBS_COMPLETED, Strategy="adaptive", WorkloadClass="CPU_BOUND")
    assert s.flush() == 2
    by_sum = sorted(d["StatisticValues"]["SampleCount"] for d in cw.datums("JobsCompleted"))
    assert by_sum == [1.0, 2.0]


def test_unitless_metric_omits_unit():
    s, cw = sink()
    s.emit(Metric.WORKER_HEALTHY, 1, WorkerId="worker-1")
    s.flush()
    assert "Unit" not in cw.datums("WorkerHealthy")[0]


def test_many_series_are_chunked_into_calls_of_twenty():
    s, cw = sink()
    for i in range(45):
        s.emit(Metric.WORKER_HEALTHY, 1, WorkerId=f"worker-{i}")
    assert s.flush() == 45
    assert [len(c["MetricData"]) for c in cw.calls] == [20, 20, 5]
    assert MAX_DATUMS_PER_CALL == 20


def test_second_flush_with_nothing_pending_makes_no_call():
    s, cw = sink()
    s.emit(Metric.JOBS_SUBMITTED, WorkloadClass="IO_BOUND")
    s.flush()
    assert s.flush() == 0
    assert len(cw.calls) == 1


def test_throttling_is_counted_dropped_and_never_raised():
    s, cw = sink(FakeCloudWatch(fail_times=1))
    s.emit(Metric.JOBS_SUBMITTED, WorkloadClass="IO_BOUND")
    assert s.flush() == 0
    assert s.failed_calls == 1
    assert s.dropped_datums == 1
    s.emit(Metric.JOBS_SUBMITTED, WorkloadClass="IO_BOUND")
    assert s.flush() == 1
    assert s.failed_calls == 1


def test_a_failed_chunk_does_not_lose_the_others():
    s, cw = sink(FakeCloudWatch(fail_times=1))
    for i in range(25):
        s.emit(Metric.WORKER_HEALTHY, 1, WorkerId=f"worker-{i}")
    assert s.flush() == 5
    assert s.dropped_datums == 20


def test_stop_does_a_final_flush():
    s, cw = sink(flush_interval_s=3600.0)
    s.start()
    s.emit(Metric.JOBS_SUBMITTED, WorkloadClass="IO_BOUND")
    assert cw.calls == []
    s.stop()
    assert len(cw.datums("JobsSubmitted")) == 1


def test_start_and_stop_are_idempotent():
    s, cw = sink()
    s.start()
    thread = s._thread
    s.start()
    assert s._thread is thread
    s.stop()
    s.stop()
    s.start()
    s.stop()
    assert cw.calls == []


def test_background_thread_flushes_on_its_interval():
    s, cw = sink(flush_interval_s=0.02)
    s.emit(Metric.JOBS_SUBMITTED, WorkloadClass="IO_BOUND")
    s.start()
    try:
        deadline = time.monotonic() + 3.0
        while not cw.calls and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        s.stop()
    assert cw.datums("JobsSubmitted")


@pytest.mark.parametrize("make", [lambda: sink()[0], InMemoryMetricsSink, NullMetricsSink])
def test_emit_rejects_unknown_metrics_and_dimensions(make):
    s = make()
    with pytest.raises(ValueError):
        s.emit("NotAMetric")
    with pytest.raises(ValueError):
        s.emit(Metric.JOBS_SUBMITTED, Strategy="fifo")  # not allowed on this metric
    with pytest.raises(ValueError):
        s.emit(Metric.QUEUE_DEPTH_VISIBLE, WorkerId="worker-1")
    with pytest.raises(ValueError):
        s.emit(Metric.WORKER_HEALTHY, WorkerId="")


def test_non_finite_values_are_ignored():
    s, cw = sink()
    s.emit(Metric.QUEUE_DEPTH_VISIBLE, float("nan"))
    s.emit(Metric.QUEUE_DEPTH_VISIBLE, float("inf"))
    assert s.flush() == 0


def test_null_sink_is_a_no_op():
    n = NullMetricsSink()
    n.start()
    n.emit(Metric.JOBS_SUBMITTED, WorkloadClass="IO_BOUND")
    assert n.flush() == 0
    n.stop()


def test_in_memory_total_and_count_span_pending_and_published():
    m = InMemoryMetricsSink()
    m.emit(Metric.JOBS_COMPLETED, Strategy="fifo", WorkloadClass="CPU_BOUND")
    m.emit(Metric.JOBS_COMPLETED, Strategy="fifo", WorkloadClass="IO_BOUND")
    assert m.flush() == 2
    m.emit(Metric.JOBS_COMPLETED, 5, Strategy="adaptive", WorkloadClass="CPU_BOUND")
    assert m.total(Metric.JOBS_COMPLETED) == 7
    assert m.count(Metric.JOBS_COMPLETED) == 3
    assert m.total(Metric.JOBS_COMPLETED, Strategy="fifo") == 2
    assert m.count(Metric.JOBS_COMPLETED, WorkloadClass="CPU_BOUND") == 2
    assert m.total(Metric.JOBS_FAILED) == 0


def test_in_memory_history_is_bounded():
    m = InMemoryMetricsSink(history=3)
    for i in range(10):
        m.emit(Metric.WORKER_HEALTHY, 1, WorkerId=f"w{i}")
    m.flush()
    assert len(m.published) == 3


def test_no_module_scope_boto3_import():
    tree = ast.parse(Path(metrics_module.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(not a.name.startswith(("boto3", "botocore")) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("boto3", "botocore"))
