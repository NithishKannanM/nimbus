"""The backend factory.

The invariant under test is architectural rather than behavioural: there is
**exactly one** place in the codebase that branches on ``NIMBUS_BACKEND``. If a
second one appears, the Phase 5 exit criterion -- the same test suite green
against ``NIMBUS_BACKEND=aws`` -- stops meaning anything, because the two
backends would no longer be exercising the same code.
"""

from __future__ import annotations

import pathlib

import pytest

from backend.config import NimbusConfig
from backend.services.artifacts.local import LocalArtifactStore
from backend.services.factory import (
    UnknownBackend,
    build_artifacts,
    build_queue,
    build_store,
    validate,
)
from backend.services.queue.local import LocalJobQueue
from backend.services.store.local import LocalStateStore

REPO = pathlib.Path(__file__).resolve().parents[2]


def local_config(**overrides) -> NimbusConfig:
    return NimbusConfig(backend="local", **overrides)


def aws_config(**overrides) -> NimbusConfig:
    defaults = {
        "backend": "aws",
        "aws_region": "us-east-1",
        "sqs_queue_url": "https://sqs.us-east-1.amazonaws.com/1234/nimbus-jobs",
        "sqs_dlq_url": "https://sqs.us-east-1.amazonaws.com/1234/nimbus-jobs-dlq",
        "dynamodb_table": "nimbus-state",
        "s3_bucket": "nimbus-artifacts",
    }
    return NimbusConfig(**{**defaults, **overrides})


# --------------------------------------------------------------------------- #
# local
# --------------------------------------------------------------------------- #
def test_local_backend_builds_local_implementations(tmp_path):
    config = local_config(artifact_dir=str(tmp_path))
    assert isinstance(build_queue(config), LocalJobQueue)
    assert isinstance(build_store(config), LocalStateStore)
    assert isinstance(build_artifacts(config), LocalArtifactStore)


def test_local_needs_no_aws_settings():
    assert validate(local_config()) == []


def test_the_dlq_threshold_sits_above_the_retry_budget():
    """SQS counts deliveries, not attempts. infra/sqs.tf uses the same
    expression, so both backends dead-letter at the same point."""
    queue = build_queue(local_config(max_attempts=3))
    assert queue._max_receive_count == 5


def test_an_unknown_backend_is_rejected():
    with pytest.raises(UnknownBackend):
        build_queue(NimbusConfig(backend="azure"))
    assert "azure" in validate(NimbusConfig(backend="azure"))[0]


# --------------------------------------------------------------------------- #
# aws
# --------------------------------------------------------------------------- #
def test_a_complete_aws_config_validates():
    assert validate(aws_config()) == []


@pytest.mark.parametrize(
    "missing, expected",
    [
        ("sqs_queue_url", "NIMBUS_SQS_QUEUE_URL"),
        ("dynamodb_table", "NIMBUS_DYNAMODB_TABLE"),
        ("s3_bucket", "NIMBUS_S3_BUCKET"),
    ],
)
def test_missing_aws_settings_are_reported(missing: str, expected: str):
    problems = validate(aws_config(**{missing: ""}))
    assert any(expected in p for p in problems)


def test_every_missing_setting_is_reported_at_once():
    """Returned rather than raised so a misconfigured deployment does not
    discover its second missing variable only after fixing the first -- on AWS
    each of those iterations is an instance restart."""
    problems = validate(aws_config(sqs_queue_url="", dynamodb_table="", s3_bucket=""))
    assert len(problems) >= 3


def test_a_missing_dlq_is_reported_because_it_stalls_silently():
    problems = validate(aws_config(sqs_dlq_url=""))
    assert any("DLQ" in p for p in problems)
    assert any("remain live" in p for p in problems)


# --------------------------------------------------------------------------- #
# the architectural invariant
# --------------------------------------------------------------------------- #
def test_only_the_factory_branches_on_the_backend():
    """One place, and this test is what keeps it one place.

    docs/architecture.md section 6: "There is exactly one place in the codebase
    that branches on backend." A second branch would mean the AWS path and the
    local path diverge somewhere other than at construction, and the equivalence
    the phase depends on would quietly stop holding.
    """
    offenders = []
    for path in sorted((REPO / "backend").rglob("*.py")) + sorted(
        (REPO / "scheduler").rglob("*.py")
    ) + sorted((REPO / "worker").rglob("*.py")) + sorted((REPO / "benchmark").rglob("*.py")):
        if path.name in ("factory.py", "config.py"):
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "config.backend" in stripped or "cfg.backend" in stripped:
                offenders.append(f"{path.relative_to(REPO)}:{number}: {stripped}")
    # app.py mounts the internal results router for remote workers; that is a
    # routing decision, not a construction one, and it is the single permitted
    # exception.
    offenders = [o for o in offenders if "api/app.py" not in o]
    assert offenders == [], "backend branching escaped the factory:\n" + "\n".join(offenders)


def test_the_aws_adapters_never_import_boto3_at_module_scope():
    """Local mode must not pay for boto3, and must not require it: the whole
    benchmark harness runs with no AWS dependency installed."""
    for relative in (
        "backend/services/queue/sqs.py",
        "backend/services/store/dynamodb.py",
        "backend/services/artifacts/s3.py",
        "backend/services/factory.py",
    ):
        source = (REPO / relative).read_text()
        for line in source.splitlines():
            if line.startswith(("import boto3", "from boto3")):
                pytest.fail(f"{relative} imports boto3 at module scope")


def test_credentials_are_never_read_from_configuration():
    """Resource addresses only. There is no path by which a key could reach a
    config file, a log line or a benchmark manifest."""
    fields = set(NimbusConfig.model_fields)
    for forbidden in (
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "secret_key",
    ):
        assert forbidden not in fields
