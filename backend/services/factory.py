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
from typing import Any

from backend.config import NimbusConfig

log = logging.getLogger("nimbus.factory")

LOCAL = "local"
AWS = "aws"
BACKENDS = (LOCAL, AWS)


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


def validate(config: NimbusConfig) -> list[str]:
    """Missing settings for the selected backend, as a list of complaints.

    Returned rather than raised so a caller can report all of them at once. A
    misconfigured deployment that discovers its second missing environment
    variable only after fixing the first is a bad way to spend an afternoon,
    and on AWS each iteration is an instance restart.
    """
    problems: list[str] = []
    try:
        backend = _check(config)
    except UnknownBackend as exc:
        return [str(exc)]
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
