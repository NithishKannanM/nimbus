"""Thin boto3 client construction. The only module that imports boto3.

Two rules hold everywhere in ``backend/aws/``:

**boto3 is imported lazily.** Nimbus runs entirely locally through Phase 4 and
the whole benchmark harness works with no AWS dependency at all. Importing boto3
at module scope would make it a hard requirement for people who will never use
it, and would put a ~1 s import on the path of every local test run. It lives in
``requirements-aws.txt`` for the same reason.

**Credentials are never constructed here.** On EC2 they come from the instance
profile; locally they come from the standard boto3 chain (environment,
``~/.aws/config``, SSO). Nimbus never reads a key out of its own configuration,
so there is no path by which a credential could reach a config file, a log line
or a benchmark manifest (docs/limitations.md section 5).
"""

from __future__ import annotations

import functools
import logging
from typing import Any

log = logging.getLogger("nimbus.aws")


class BotoUnavailable(RuntimeError):
    """boto3 is not installed. Raised with the fix, not just the symptom."""

    def __init__(self) -> None:
        super().__init__(
            "boto3 is required for NIMBUS_BACKEND=aws but is not installed. "
            "Install it with: pip install -r requirements-aws.txt"
        )


def _boto3() -> Any:
    try:
        import boto3  # noqa: PLC0415 -- deliberately lazy; see the module docstring
    except ImportError as exc:  # pragma: no cover -- depends on the environment
        raise BotoUnavailable() from exc
    return boto3


@functools.lru_cache(maxsize=None)
def _session(region: str | None, profile: str | None) -> Any:
    boto3 = _boto3()
    return boto3.Session(region_name=region, profile_name=profile)


def client(service: str, *, region: str | None = None, profile: str | None = None) -> Any:
    """A boto3 client for one service.

    Sessions are cached per (region, profile). Client construction parses the
    service model from disk and takes tens of milliseconds; doing it per call
    would show up in ``decision_latency_us``, and a scheduling-latency metric
    that is really measuring boto3 startup is worse than no metric.
    """
    return _session(region, profile).client(service)


@functools.lru_cache(maxsize=None)
def resource(service: str, *, region: str | None = None, profile: str | None = None) -> Any:
    """A boto3 resource. Used only for DynamoDB, whose resource API does the
    Python <-> DynamoDB type marshalling that the raw client leaves to you."""
    return _session(region, profile).resource(service)


def reset_clients() -> None:
    """Drop cached sessions and resources. For tests that swap credentials."""
    _session.cache_clear()
    resource.cache_clear()
