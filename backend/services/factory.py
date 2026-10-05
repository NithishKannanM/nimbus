"""The one place in the codebase that branches on ``NIMBUS_BACKEND``.

That is the whole design constraint, stated in docs/architecture.md section 6:
every AWS dependency sits behind an interface with a local implementation, and
swapping the implementation must not change a single line of scheduler or worker
logic. If a second `if backend == "aws"` appears anywhere else, the constraint
has been broken and the Phase 5 exit criterion -- the same test suite green
against ``NIMBUS_BACKEND=aws`` -- stops meaning anything.

The AWS constructors are imported inside the branch, not at module scope. Local
mode must not pay for boto3, and more importantly must not *require* it: the
entire benchmark harness runs with no AWS dependency installed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from backend.config import NimbusConfig

log = logging.getLogger("nimbus.factory")

LOCAL = "local"
AWS = "aws"
BACKENDS = (LOCAL, AWS)

# Data-plane transport, orthogonal to the backend: how the scheduler reaches
# its workers, not where state lives.
INPROCESS = "inprocess"
HTTP = "http"
TRANSPORTS = (INPROCESS, HTTP)


class UnknownBackend(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"unknown backend {name!r}; expected one of {', '.join(BACKENDS)}")


def _check(config: NimbusConfig) -> str:
    backend = (config.backend or LOCAL).strip().lower()
    if backend not in BACKENDS:
        raise UnknownBackend(config.backend)
    return backend


def build_queue(config: NimbusConfig) -> Any:
    if _check(config) == LOCAL:
        from backend.services.queue.local import LocalJobQueue  # noqa: PLC0415

        return LocalJobQueue(
            visibility_timeout_s=config.visibility_timeout_s,
            # SQS ApproximateReceiveCount counts *deliveries*, not *attempts*.
            # The DLQ threshold sits above the retry budget for that reason,
            # and infra/sqs.tf sets the redrive policy's maxReceiveCount to the
            # same expression so the two backends dead-letter at the same point.
            max_receive_count=config.max_attempts + 2,
        )

    from backend.services.queue.sqs import SQSJobQueue  # noqa: PLC0415

    return SQSJobQueue(
        config.sqs_queue_url,
        dlq_url=config.sqs_dlq_url or None,
        visibility_timeout_s=config.visibility_timeout_s,
        region=config.aws_region or None,
    )


def build_store(config: NimbusConfig) -> Any:
    if _check(config) == LOCAL:
        from backend.services.store.local import LocalStateStore  # noqa: PLC0415

        return LocalStateStore()

    from backend.services.store.dynamodb import DynamoDBStateStore  # noqa: PLC0415

    return DynamoDBStateStore(config.dynamodb_table, region=config.aws_region or None)


def build_artifacts(config: NimbusConfig) -> Any:
    if _check(config) == LOCAL:
        from backend.services.artifacts.local import LocalArtifactStore  # noqa: PLC0415

        return LocalArtifactStore(config.artifact_dir)

    from backend.services.artifacts.s3 import S3ArtifactStore  # noqa: PLC0415

    return S3ArtifactStore(config.s3_bucket, region=config.aws_region or None)


def build_metrics(config: NimbusConfig) -> Any:
    from backend.services.metrics import (  # noqa: PLC0415
        CloudWatchMetricsSink,
        InMemoryMetricsSink,
        NullMetricsSink,
    )

    if not config.metrics_enabled:
        return NullMetricsSink()
    if _check(config) == LOCAL:
        return InMemoryMetricsSink()
    return CloudWatchMetricsSink(
        region=config.aws_region or None,
        flush_interval_s=config.metrics_flush_interval_s,
    )


def _check_transport(config: NimbusConfig) -> str:
    transport = (config.worker_transport or INPROCESS).strip().lower()
    if transport not in TRANSPORTS:
        raise ValueError(
            f"unknown worker transport {config.worker_transport!r}; "
            f"expected one of {', '.join(TRANSPORTS)}"
        )
    return transport


def build_workers(
    config: NimbusConfig,
    *,
    sink: Callable[[Any], None],
    cores_for: Callable[[int], list[int] | None],
    reservations: Callable[[str], int],
    transport_for: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """The fleet, unstarted, keyed by worker id.

    ``inprocess`` is the local process-pool fleet. ``http`` is one
    RemoteWorkerClient per entry in ``worker_endpoints`` -- under http that
    list, not ``worker_count``, defines the fleet. ``transport_for`` is a test
    seam: worker_id -> httpx transport (None for the real network).
    """
    if _check_transport(config) == INPROCESS:
        from worker.agent import LocalWorkerAgent  # noqa: PLC0415

        workers: dict[str, Any] = {}
        for index in range(config.worker_count):
            worker_id = f"worker-{index + 1}"
            workers[worker_id] = LocalWorkerAgent(
                worker_id,
                slots=config.worker_slots,
                cores=cores_for(index),
                sink=sink,
            )
        return workers

    from backend.services.remote import RemoteWorkerClient, parse_worker_endpoints  # noqa: PLC0415

    return {
        worker_id: RemoteWorkerClient(
            worker_id,
            url,
            slots=config.worker_slots,
            reservations=reservations,
            dispatch_timeout_s=config.dispatch_timeout_s,
            probe_timeout_s=config.worker_probe_timeout_s,
            transport=transport_for(worker_id) if transport_for else None,
        )
        for worker_id, url in parse_worker_endpoints(config.worker_endpoints).items()
    }


def build_telemetry_collector(
    config: NimbusConfig,
    registry: Any,
    *,
    workers: Callable[[], Any],
    pid_provider: Callable[[str], Any],
    active_jobs_provider: Callable[[str], int],
    is_alive: Callable[[str], bool],
) -> Any:
    if _check_transport(config) == INPROCESS:
        from worker.telemetry import TelemetryCollector  # noqa: PLC0415

        return TelemetryCollector(
            registry,
            interval_s=config.telemetry_interval_s,
            pid_provider=pid_provider,
            active_jobs_provider=active_jobs_provider,
        )

    from backend.services.remote import RemoteTelemetryCollector  # noqa: PLC0415

    return RemoteTelemetryCollector(
        registry,
        workers=workers,
        interval_s=config.telemetry_interval_s,
        is_alive=is_alive,
    )


def validate(config: NimbusConfig) -> list[str]:
    """Missing settings for the selected backend, as a list of complaints.

    Returned rather than raised so a caller can report all of them at once. A
    misconfigured deployment that discovers its second missing environment
    variable only after fixing the first is a bad way to spend an afternoon,
    and on AWS each iteration is an instance restart.
    """
    problems: list[str] = []
    try:
        transport = _check_transport(config)
    except ValueError as exc:
        problems.append(str(exc))
    else:
        if transport == HTTP:
            from backend.services.remote import parse_worker_endpoints  # noqa: PLC0415

            if not (config.worker_endpoints or "").strip():
                problems.append("NIMBUS_WORKER_ENDPOINTS is required for worker_transport=http")
            else:
                try:
                    parse_worker_endpoints(config.worker_endpoints)
                except ValueError as exc:
                    problems.append(f"NIMBUS_WORKER_ENDPOINTS is invalid: {exc}")
    try:
        backend = _check(config)
    except UnknownBackend as exc:
        return [*problems, str(exc)]
    if backend == LOCAL:
        return problems
    if not config.sqs_queue_url:
        problems.append("NIMBUS_SQS_QUEUE_URL is required for backend=aws")
    if not config.dynamodb_table:
        problems.append("NIMBUS_DYNAMODB_TABLE is required for backend=aws")
    if not config.s3_bucket:
        problems.append("NIMBUS_S3_BUCKET is required for backend=aws")
    if not config.sqs_dlq_url:
        # Not fatal: the queue works without it. But dead-lettered jobs would
        # never have their records terminated, so they would sit live forever
        # with nothing left to deliver them -- a silent stall, which is worse
        # than a loud failure and worth warning about.
        problems.append(
            "NIMBUS_SQS_DLQ_URL is unset: dead-lettered jobs will not be "
            "reconciled and will remain live in the store"
        )
    return problems
