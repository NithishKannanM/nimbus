"""DynamoDB-backed state store, single-table (docs/architecture.md section 7.1).

The access patterns were enumerated before the keys were chosen, and the key
design falls out of them:

| Entity   | PK                    | SK                    | Serves |
|----------|-----------------------|-----------------------|--------|
| Job      | ``JOB#{job_id}``      | ``META``              | A1, A2 |
| Decision | ``JOB#{job_id}``      | ``DECISION#{n:03d}``  | A6     |
| Run ref  | ``RUN#{run_id}``      | ``JOB#{job_id}``      | A4     |

with two GSIs for the two patterns that would otherwise tempt a scan:

- **GSI-1 ``status-index``** — ``STATUS#{status}`` / ``{created_at}#{job_id}``
  serves A3, "list jobs by status, newest first", as a descending query.
- **GSI-2 ``decision-index``** — ``DECISIONS#{yyyy-mm-dd}`` / ``{decided_at}#{job_id}``
  serves A7, "recent decisions across all jobs". Partitioning by day rather
  than by a constant is what stops it becoming a single hot partition that
  every placement writes to.

``LocalStateStore`` already raises ``ConditionFailed`` with DynamoDB's exact
semantics, so the concurrency behaviour of the two backends is the same and the
engine needs no branch. That was the reason for building the local one that way.

**Not implemented here:** telemetry samples (A9) and runtime stats (A10).
Telemetry on the placement hot path is served from the in-memory registry in
both backends, deliberately -- a network round-trip per placement would make
``decision_latency_us`` a measurement of DynamoDB rather than of the scheduler
(section 7.1). Durable telemetry is an archive concern and goes to S3 with the
rest of a run's artifacts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from backend.models.decision import SchedulingDecision
from backend.models.enums import JobStatus
from backend.models.job import Job
from backend.services.store.base import ConditionFailed

log = logging.getLogger("nimbus.store.dynamodb")

STATUS_INDEX = "status-index"
DECISION_INDEX = "decision-index"


def _to_dynamo(value: Any) -> Any:
    """Make a pydantic dump storable.

    DynamoDB has no float type -- the SDK rejects them outright rather than
    rounding silently, which is the right call -- so every float becomes a
    Decimal. Going through ``str`` rather than ``Decimal(float)`` avoids
    materialising the full binary expansion of the float (``Decimal(0.1)`` is
    fifty-odd digits), which DynamoDB would reject for exceeding 38 digits of
    precision.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    """Inverse of :func:`_to_dynamo`. Decimals come back as floats or ints."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


class DynamoDBStateStore:
    def __init__(
        self,
        table_name: str,
        *,
        region: str | None = None,
        table: Any = None,
    ) -> None:
        if not table_name:
            raise ValueError("table_name is required for the AWS backend")
        self._name = table_name
        if table is None:
            from backend.aws.clients import resource  # noqa: PLC0415

            table = resource("dynamodb", region=region).Table(table_name)
        self._table = table

    # ------------------------------------------------------------------ #
    # keys
    # ------------------------------------------------------------------ #
    @staticmethod
    def _job_key(job_id: str) -> dict[str, str]:
        return {"PK": f"JOB#{job_id}", "SK": "META"}

    @staticmethod
    def _decision_key(decision: SchedulingDecision, sequence: int) -> dict[str, str]:
        return {"PK": f"JOB#{decision.job_id}", "SK": f"DECISION#{sequence:03d}"}

    # ------------------------------------------------------------------ #
    # A1 / A2 -- job by id, conditional update
    # ------------------------------------------------------------------ #
    def _job_item(self, job: Job) -> dict[str, Any]:
        item = _to_dynamo(job.model_dump(mode="json"))
        item.update(self._job_key(job.job_id))
        item["entity"] = "JOB"
        # GSI-1: newest-first listing by status without a scan. The sort key
        # carries job_id as a tiebreak so two jobs created in the same
        # microsecond still get distinct, stable ordering.
        item["GSI1PK"] = f"STATUS#{job.status.value}"
        item["GSI1SK"] = f"{job.created_at.isoformat()}#{job.job_id}"
        if job.run_id:
            item["run_id"] = job.run_id
        return item

    def put_job(self, job: Job) -> None:
        try:
            self._table.put_item(
                Item=self._job_item(job),
                # Submission must not silently overwrite an existing job.
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, f"{job.job_id} already exists") from exc

        if job.run_id:
            # A4: the run's job list. A separate item rather than a GSI because
            # this is written once and read in bulk exactly once, at analysis
            # time -- an index would be paid for on every job write instead.
            self._table.put_item(
                Item={
                    "PK": f"RUN#{job.run_id}",
                    "SK": f"JOB#{job.job_id}",
                    "entity": "RUN_JOB",
                    "job_id": job.job_id,
                }
            )

    def get_job(self, job_id: str) -> Job | None:
        item = self._table.get_item(Key=self._job_key(job_id)).get("Item")
        return None if item is None else self._as_job(item)

    def update_job(self, job: Job, *, expect_status: JobStatus) -> None:
        """Conditional write on the expected current status.

        This is the store-side half of the idempotency guarantee
        (docs/architecture.md section 8, guard 3): a late result from a
        superseded attempt fails the condition and is discarded rather than
        overwriting the record of the attempt that replaced it.
        """
        try:
            self._table.put_item(
                Item=self._job_item(job),
                ConditionExpression="attribute_exists(PK) AND #s = :expected",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":expected": expect_status.value},
            )
        except Exception as exc:  # noqa: BLE001
            raise self._translate(
                exc, f"{job.job_id}: expected status {expect_status}"
            ) from exc

    # ------------------------------------------------------------------ #
    # A3 / A4 -- listings
    # ------------------------------------------------------------------ #
    def list_jobs(
        self, *, status: JobStatus | None = None, run_id: str | None = None, limit: int = 100
    ) -> list[Job]:
        if status is not None:
            return self._list_by_status(status, run_id=run_id, limit=limit)
        if run_id is not None:
            return self._list_by_run(run_id, limit=limit)
        # No filter means "every job", which is the one request that genuinely
        # needs a scan. The API never issues it -- both its list endpoints pass
        # a status or a run_id -- and the reaper walks the live statuses one at
        # a time for exactly this reason.
        raise ValueError(
            "listing every job would require a table scan; pass status= or run_id="
        )

    def _list_by_status(
        self, status: JobStatus, *, run_id: str | None, limit: int
    ) -> list[Job]:
        jobs: list[Job] = []
        kwargs: dict[str, Any] = {
            "IndexName": STATUS_INDEX,
            "KeyConditionExpression": "GSI1PK = :pk",
            "ExpressionAttributeValues": {":pk": f"STATUS#{status.value}"},
            "ScanIndexForward": False,  # newest first
            "Limit": limit,
        }
        while len(jobs) < limit:
            page = self._table.query(**kwargs)
            for item in page.get("Items", []):
                job = self._as_job(item)
                if run_id is not None and job.run_id != run_id:
                    continue
                jobs.append(job)
                if len(jobs) >= limit:
                    break
            token = page.get("LastEvaluatedKey")
            if not token:
                break
            kwargs["ExclusiveStartKey"] = token
        return jobs

    def _list_by_run(self, run_id: str, *, limit: int) -> list[Job]:
        refs = self._table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={":pk": f"RUN#{run_id}", ":sk": "JOB#"},
            Limit=limit,
        ).get("Items", [])
        jobs = [self.get_job(ref["job_id"]) for ref in refs]
        return [job for job in jobs if job is not None]

    def counts_by_status(self) -> dict[str, int]:
        """One bounded query per status.

        DynamoDB has no aggregate query, so the honest options are a counter
        item maintained transactionally on every transition, or this. This is
        chosen because it cannot drift: a counter that a failed transition
        forgets to decrement is worse than a slightly slower read, and the
        engine's drain check reads these numbers to decide whether a benchmark
        run has finished.
        """
        counts: dict[str, int] = {}
        for status in JobStatus:
            response = self._table.query(
                IndexName=STATUS_INDEX,
                KeyConditionExpression="GSI1PK = :pk",
                ExpressionAttributeValues={":pk": f"STATUS#{status.value}"},
                Select="COUNT",
            )
            counts[status.value] = int(response.get("Count", 0))
        return counts

    # ------------------------------------------------------------------ #
    # A5 / A6 / A7 -- decisions
    # ------------------------------------------------------------------ #
    def put_decision(self, decision: SchedulingDecision) -> None:
        sequence = self._next_decision_sequence(decision.job_id)
        item = _to_dynamo(decision.model_dump(mode="json"))
        item.update(self._decision_key(decision, sequence))
        item["entity"] = "DECISION"
        decided = getattr(decision, "decided_at", None) or datetime.now(timezone.utc)
        day = decided.date().isoformat() if isinstance(decided, datetime) else str(decided)[:10]
        # GSI-2, partitioned by day. A constant partition key would make every
        # placement in the project's lifetime write to one partition, which is
        # the textbook hot-key mistake and would throttle the scheduler itself.
        item["GSI2PK"] = f"DECISIONS#{day}"
        item["GSI2SK"] = f"{decided.isoformat() if isinstance(decided, datetime) else decided}#{decision.job_id}"
        self._table.put_item(Item=item)

    def _next_decision_sequence(self, job_id: str) -> int:
        response = self._table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={":pk": f"JOB#{job_id}", ":sk": "DECISION#"},
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        return 0 if not items else int(items[0]["SK"].split("#")[1]) + 1

    def list_decisions(
        self, *, job_id: str | None = None, limit: int = 50
    ) -> list[SchedulingDecision]:
        if job_id is not None:
            items = self._table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={":pk": f"JOB#{job_id}", ":sk": "DECISION#"},
                ScanIndexForward=False,
                Limit=limit,
            ).get("Items", [])
            return [self._as_decision(i) for i in items]

        # A7: recent decisions across all jobs. Walk day partitions backwards
        # from today until the limit is met -- bounded by the number of days
        # queried, never a scan.
        found: list[SchedulingDecision] = []
        today = datetime.now(timezone.utc).date()
        for offset in range(7):
            day = (today.toordinal() - offset)
            key = f"DECISIONS#{datetime.fromordinal(day).date().isoformat()}"
            items = self._table.query(
                IndexName=DECISION_INDEX,
                KeyConditionExpression="GSI2PK = :pk",
                ExpressionAttributeValues={":pk": key},
                ScanIndexForward=False,
                Limit=limit - len(found),
            ).get("Items", [])
            found.extend(self._as_decision(i) for i in items)
            if len(found) >= limit:
                break
        return found[:limit]

    # ------------------------------------------------------------------ #
    # marshalling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _strip(item: dict[str, Any]) -> dict[str, Any]:
        """Drop the key and index attributes before rebuilding a model.

        The models use ``extra`` defaults, so leaving ``PK``/``GSI1PK`` in place
        would either be silently absorbed or raise, depending on the model --
        and either way they are storage concerns that have no business in a
        domain object.
        """
        drop = {"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "entity", "ttl"}
        return {k: v for k, v in item.items() if k not in drop}

    def _as_job(self, item: dict[str, Any]) -> Job:
        return Job.model_validate(_from_dynamo(self._strip(item)))

    def _as_decision(self, item: dict[str, Any]) -> SchedulingDecision:
        return SchedulingDecision.model_validate(_from_dynamo(self._strip(item)))

    @staticmethod
    def _translate(exc: Exception, message: str) -> Exception:
        """Map DynamoDB's conditional failure onto ``ConditionFailed``.

        The engine's concurrency handling is written entirely against that
        exception, and the local store already raises it. Leaking a
        ``ClientError`` here would turn an ordinary lost race -- which the
        engine handles by yielding the job to whoever won -- into an unhandled
        exception in the scheduler loop.
        """
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException" or "ConditionalCheckFailed" in type(exc).__name__:
            return ConditionFailed(message)
        return exc
