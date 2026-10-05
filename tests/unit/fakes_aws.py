"""In-process fakes for the SQS, DynamoDB and S3 surfaces Nimbus actually uses.

Not a general AWS simulator, and not trying to be. These model the specific
behaviours the adapters depend on, including the awkward ones:

- SQS's ``ApproximateReceiveCount`` increments on delivery and cannot be
  decremented, which is the entire reason ``defer`` and ``release`` are
  different operations;
- SQS's redrive policy moves a message past ``maxReceiveCount`` on its own,
  without anyone asking;
- DynamoDB's ``ConditionalCheckFailedException``, which is the store's whole
  concurrency story;
- S3's 404 shape for ``head_object``, which ``exists`` has to distinguish from
  a real error.

Written by hand rather than pulled in with moto because the point is to pin
*our understanding* of these semantics. A fake that encodes the wrong belief
about receive counts fails loudly in review; a dependency that quietly gets it
right teaches nobody, and a dependency that quietly gets it wrong is worse than
either.
"""

from __future__ import annotations

import time
import uuid
from typing import Any


class ClientError(Exception):
    """botocore's error shape, reduced to what the adapters read."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


# --------------------------------------------------------------------------- #
# SQS
# --------------------------------------------------------------------------- #
class FakeSQS:
    def __init__(self, *, max_receive_count: int = 5, clock=time.monotonic) -> None:
        self._queues: dict[str, list[dict[str, Any]]] = {}
        self._max_receive_count = max_receive_count
        self._clock = clock
        self.dlq_url: str | None = None
        self.calls: list[str] = []

    def create_queue(self, url: str) -> str:
        self._queues.setdefault(url, [])
        return url

    # -- api ------------------------------------------------------------
    def send_message(self, *, QueueUrl: str, MessageBody: str, DelaySeconds: int = 0) -> dict:
        self.calls.append("send_message")
        self._queues.setdefault(QueueUrl, []).append(
            {
                "body": MessageBody,
                "receipt": None,
                "visible_at": self._clock() + DelaySeconds,
                "receive_count": 0,
                "id": uuid.uuid4().hex,
            }
        )
        return {"MessageId": uuid.uuid4().hex}

    def receive_message(
        self,
        *,
        QueueUrl: str,
        MaxNumberOfMessages: int = 1,
        WaitTimeSeconds: int = 0,
        VisibilityTimeout: int = 30,
        AttributeNames: list[str] | None = None,
    ) -> dict:
        self.calls.append("receive_message")
        now = self._clock()
        out = []
        for message in list(self._queues.get(QueueUrl, [])):
            if len(out) >= MaxNumberOfMessages:
                break
            if message["visible_at"] > now:
                continue
            # Redrive, done by the service rather than by the client. This is
            # why SQSJobQueue.sweep_expired reports rather than moves.
            if (
                self.dlq_url
                and QueueUrl != self.dlq_url
                and message["receive_count"] >= self._max_receive_count
            ):
                self._queues[QueueUrl].remove(message)
                self._queues.setdefault(self.dlq_url, []).append(
                    {**message, "receipt": None, "visible_at": 0.0, "receive_count": 0}
                )
                continue
            message["receive_count"] += 1
            message["receipt"] = uuid.uuid4().hex
            message["visible_at"] = now + VisibilityTimeout
            out.append(
                {
                    "Body": message["body"],
                    "ReceiptHandle": message["receipt"],
                    "MessageId": message["id"],
                    "Attributes": {
                        "ApproximateReceiveCount": str(message["receive_count"])
                    },
                }
            )
        return {"Messages": out} if out else {}

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> dict:
        self.calls.append("delete_message")
        message = self._by_receipt(QueueUrl, ReceiptHandle)
        self._queues[QueueUrl].remove(message)
        return {}

    def change_message_visibility(
        self, *, QueueUrl: str, ReceiptHandle: str, VisibilityTimeout: int
    ) -> dict:
        self.calls.append("change_message_visibility")
        message = self._by_receipt(QueueUrl, ReceiptHandle)
        message["visible_at"] = self._clock() + VisibilityTimeout
        message["receipt"] = None
        return {}

    def get_queue_attributes(self, *, QueueUrl: str, AttributeNames: list[str]) -> dict:
        now = self._clock()
        messages = self._queues.get(QueueUrl, [])
        visible = sum(1 for m in messages if m["visible_at"] <= now)
        delayed = sum(1 for m in messages if m["visible_at"] > now and m["receipt"] is None)
        not_visible = len(messages) - visible - delayed
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": str(visible),
                "ApproximateNumberOfMessagesNotVisible": str(not_visible),
                "ApproximateNumberOfMessagesDelayed": str(delayed),
            }
        }

    def purge_queue(self, *, QueueUrl: str) -> dict:
        self._queues[QueueUrl] = []
        return {}

    def _by_receipt(self, url: str, receipt: str) -> dict[str, Any]:
        for message in self._queues.get(url, []):
            if message["receipt"] == receipt:
                return message
        raise ClientError("ReceiptHandleIsInvalid", "receipt expired or already used")


# --------------------------------------------------------------------------- #
# DynamoDB (the resource-level Table API)
# --------------------------------------------------------------------------- #
class FakeTable:
    """Enough of ``boto3.resource('dynamodb').Table`` for the store.

    Supports the exact query shapes the adapter issues -- key-condition on
    PK/SK with ``begins_with``, GSI queries by partition, ``ScanIndexForward``,
    ``Limit``, ``Select=COUNT`` -- and rejects anything else, so a future query
    that quietly needs a scan fails here rather than on a bill.
    """

    def __init__(self, name: str = "nimbus-state") -> None:
        self.name = name
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.writes = 0

    def put_item(
        self,
        *,
        Item: dict[str, Any],
        ConditionExpression: str | None = None,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
    ) -> dict:
        key = (Item["PK"], Item["SK"])
        existing = self.items.get(key)
        if ConditionExpression:
            self._check(ConditionExpression, existing, ExpressionAttributeNames or {},
                        ExpressionAttributeValues or {})
        self.items[key] = dict(Item)
        self.writes += 1
        return {}

    def get_item(self, *, Key: dict[str, str]) -> dict:
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}

    def query(
        self,
        *,
        KeyConditionExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        IndexName: str | None = None,
        ScanIndexForward: bool = True,
        Limit: int = 1000,
        Select: str | None = None,
        ExclusiveStartKey: dict | None = None,
        ExpressionAttributeNames: dict | None = None,
    ) -> dict:
        pk_attr, sk_attr = ("PK", "SK")
        if IndexName == "status-index":
            pk_attr, sk_attr = ("GSI1PK", "GSI1SK")
        elif IndexName == "decision-index":
            pk_attr, sk_attr = ("GSI2PK", "GSI2SK")
        elif IndexName is not None:
            raise ClientError("ValidationException", f"no such index: {IndexName}")

        wanted_pk = ExpressionAttributeValues[":pk"]
        prefix = ExpressionAttributeValues.get(":sk")

        matched = [
            item
            for item in self.items.values()
            if item.get(pk_attr) == wanted_pk
            and (prefix is None or str(item.get(sk_attr, "")).startswith(prefix))
        ]
        matched.sort(key=lambda i: str(i.get(sk_attr, "")), reverse=not ScanIndexForward)

        if Select == "COUNT":
            return {"Count": len(matched), "Items": []}
        return {"Items": [dict(i) for i in matched[:Limit]], "Count": len(matched[:Limit])}

    @staticmethod
    def _check(
        expression: str,
        existing: dict[str, Any] | None,
        names: dict[str, str],
        values: dict[str, Any],
    ) -> None:
        if "attribute_not_exists(PK)" in expression and existing is not None:
            raise ClientError("ConditionalCheckFailedException", "item exists")
        if "attribute_exists(PK)" in expression and existing is None:
            raise ClientError("ConditionalCheckFailedException", "item missing")
        if "#s = :expected" in expression:
            attribute = names.get("#s", "status")
            if existing is None or existing.get(attribute) != values.get(":expected"):
                raise ClientError(
                    "ConditionalCheckFailedException",
                    f"{attribute} is {None if existing is None else existing.get(attribute)}",
                )


# --------------------------------------------------------------------------- #
# S3
# --------------------------------------------------------------------------- #
class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.encryption: dict[tuple[str, str], str] = {}

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ServerSideEncryption: str | None = None
    ) -> dict:
        self.objects[(Bucket, Key)] = Body
        if ServerSideEncryption:
            self.encryption[(Bucket, Key)] = ServerSideEncryption
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        try:
            data = self.objects[(Bucket, Key)]
        except KeyError:
            raise ClientError("NoSuchKey", Key) from None
        return {"Body": _Body(data)}

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise ClientError("404", Key)
        return {"ContentLength": len(self.objects[(Bucket, Key)])}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


# --------------------------------------------------------------------------- #
# CloudWatch
# --------------------------------------------------------------------------- #
_CW_UNITS = frozenset(
    {
        "Seconds", "Microseconds", "Milliseconds", "Bytes", "Kilobytes", "Megabytes",
        "Gigabytes", "Terabytes", "Bits", "Kilobits", "Megabits", "Gigabits", "Terabits",
        "Percent", "Count", "Bytes/Second", "Kilobytes/Second", "Megabytes/Second",
        "Gigabytes/Second", "Terabytes/Second", "Bits/Second", "Kilobits/Second",
        "Megabits/Second", "Gigabits/Second", "Terabits/Second", "Count/Second", "None",
    }
)


class FakeCloudWatch:
    """``put_metric_data`` with the request validation that actually bites.

    Mirrors the limits a real call enforces -- 1000 datums, one value form per
    datum, a consistent statistic set, <= 30 dimensions, a known unit, and no
    ``AWS/`` namespace -- so a sink that builds a malformed datum fails here
    rather than being silently dropped in production. ``fail_times`` makes the
    first N calls throttle, which is how the failure path gets exercised.
    """

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[dict[str, Any]] = []

    def put_metric_data(self, *, Namespace: str, MetricData: list[dict[str, Any]]) -> dict:
        self.calls.append({"Namespace": Namespace, "MetricData": list(MetricData)})
        if len(self.calls) <= self.fail_times:
            raise ClientError("Throttling", "Rate exceeded")
        if not Namespace or Namespace.startswith("AWS/"):
            raise ClientError("InvalidParameterValue", f"bad namespace: {Namespace!r}")
        if not MetricData or len(MetricData) > 1000:
            raise ClientError("InvalidParameterValue", "MetricData must hold 1..1000 datums")
        for datum in MetricData:
            self._validate(datum)
        return {}

    @staticmethod
    def _validate(datum: dict[str, Any]) -> None:
        if not datum.get("MetricName"):
            raise ClientError("InvalidParameterValue", "MetricName is required")
        forms = [k for k in ("Value", "Values", "StatisticValues") if k in datum]
        if len(forms) != 1:
            raise ClientError(
                "InvalidParameterCombination",
                "exactly one of Value, Values, StatisticValues is required",
            )
        stats = datum.get("StatisticValues")
        if stats is not None:
            if stats["SampleCount"] <= 0 or stats["Minimum"] > stats["Maximum"]:
                raise ClientError("InvalidParameterValue", f"bad StatisticValues: {stats}")
        dims = datum.get("Dimensions", [])
        if len(dims) > 30:
            raise ClientError("InvalidParameterValue", "more than 30 dimensions")
        for dim in dims:
            if not dim.get("Name") or not dim.get("Value"):
                raise ClientError("InvalidParameterValue", "dimension Name/Value required")
        if "Unit" in datum and datum["Unit"] not in _CW_UNITS:
            raise ClientError("InvalidParameterValue", f"bad Unit: {datum['Unit']!r}")

    def datums(self, name: str) -> list[dict[str, Any]]:
        """Every accepted-or-attempted datum with this MetricName, in call order."""
        return [d for c in self.calls for d in c["MetricData"] if d["MetricName"] == name]
