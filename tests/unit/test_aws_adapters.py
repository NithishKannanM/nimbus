"""The Phase 5 adapters: SQS, DynamoDB, S3.

These assert that the AWS implementations honour the *same contracts* the local
ones do, because that equivalence is the entire Phase 5 exit criterion -- the
same test suite green against ``NIMBUS_BACKEND=aws``. Where the two genuinely
cannot be equivalent (SQS's non-atomic deferral, DynamoDB's inability to list
without a key), the test says so and pins the difference rather than hiding it.

boto3 is never imported. Every adapter takes an injected client, which is what
makes that possible and is also what makes the adapters testable at all without
spending money.
"""

from __future__ import annotations

import pytest

from backend.models.enums import JobStatus, WorkloadClass
from backend.models.job import Job
from backend.services.artifacts.s3 import S3ArtifactStore
from backend.services.queue.base import LeaseExpired
from backend.services.queue.sqs import SQSJobQueue
from backend.services.store.base import ConditionFailed
from backend.services.store.dynamodb import DynamoDBStateStore
from tests.unit.fakes_aws import FakeS3, FakeSQS, FakeTable

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/1234/nimbus-jobs"
DLQ_URL = "https://sqs.us-east-1.amazonaws.com/1234/nimbus-jobs-dlq"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sqs(clock: FakeClock) -> FakeSQS:
    fake = FakeSQS(max_receive_count=5, clock=clock)
    fake.create_queue(QUEUE_URL)
    fake.create_queue(DLQ_URL)
    fake.dlq_url = DLQ_URL
    return fake


@pytest.fixture
def queue(sqs: FakeSQS) -> SQSJobQueue:
    return SQSJobQueue(
        QUEUE_URL, dlq_url=DLQ_URL, visibility_timeout_s=30.0, client=sqs
    )


def make_job(**overrides) -> Job:
    return Job(
        job_type="prime_count",
        workload_class=WorkloadClass.CPU_BOUND,
        payload={"limit": 100_000},
        **overrides,
    )


# --------------------------------------------------------------------------- #
# SQS queue
# --------------------------------------------------------------------------- #
def test_enqueue_and_receive_round_trip(queue: SQSJobQueue):
    queue.enqueue("job-a")
    leases = queue.receive(max_messages=1)
    assert [lease.job_id for lease in leases] == ["job-a"]
    assert leases[0].receive_count == 1


def test_ack_removes_the_message(queue: SQSJobQueue):
    queue.enqueue("job-a")
    queue.ack(queue.receive(1)[0])
    assert queue.depth().total == 0


def test_a_stale_receipt_raises_lease_expired(queue: SQSJobQueue, clock: FakeClock):
    """SQS returns ReceiptHandleIsInvalid. The engine's entire retry path is
    written against LeaseExpired, and letting a ClientError through would turn
    an expected condition into an unhandled exception in the scheduler loop."""
    queue.enqueue("job-a")
    stale = queue.receive(1)[0]
    clock.advance(31.0)
    queue.receive(1)  # someone else takes it

    with pytest.raises(LeaseExpired):
        queue.ack(stale)
    with pytest.raises(LeaseExpired):
        queue.release(stale, 1.0)


def test_release_keeps_the_delivery_count(queue: SQSJobQueue):
    """A failed attempt. The count is the redrive policy's input and must stand."""
    queue.enqueue("job-a")
    queue.release(queue.receive(1)[0], 0.0)
    assert queue.receive(1)[0].receive_count == 2


def test_defer_resets_the_delivery_count(queue: SQSJobQueue):
    """Not a failure -- the fleet had no room.

    ApproximateReceiveCount cannot be decremented; there is no API for it. The
    only way to reset it is delete-and-resend, which is what defer does and why
    it is the one operation here that is not atomic.
    """
    queue.enqueue("job-a")
    queue.defer(queue.receive(1)[0], 0.0)
    assert queue.receive(1)[0].receive_count == 1


def test_defer_is_a_delete_followed_by_a_send(queue: SQSJobQueue, sqs: FakeSQS):
    """Pinning the non-atomicity rather than hiding it.

    A crash between the two calls loses the job. That gap cannot be closed on
    SQS, only bounded -- the reaper's orphan sweep is the backstop, because the
    job is still live in DynamoDB with no lease.
    """
    queue.enqueue("job-a")
    sqs.calls.clear()
    queue.defer(queue.receive(1)[0], 0.0)
    assert sqs.calls == ["receive_message", "delete_message", "send_message"]


def test_nack_makes_a_message_immediately_visible(queue: SQSJobQueue):
    queue.enqueue("job-a")
    queue.nack(queue.receive(1)[0])
    assert queue.receive(1) != []


def test_release_honours_its_backoff_delay(queue: SQSJobQueue, clock: FakeClock):
    queue.enqueue("job-a")
    queue.release(queue.receive(1)[0], 10.0)
    assert queue.receive(1) == []
    clock.advance(11.0)
    assert queue.receive(1) != []


def test_extend_renews_the_lease(queue: SQSJobQueue, clock: FakeClock):
    queue.enqueue("job-a")
    lease = queue.receive(1)[0]
    clock.advance(20.0)
    queue.extend(lease, 30.0)
    clock.advance(20.0)
    assert queue.receive(1) == [], "the extended lease lapsed early"


def test_delay_is_clamped_to_the_sqs_maximum(queue: SQSJobQueue, sqs: FakeSQS):
    """SQS rejects DelaySeconds above 900. Clamping beats a rejected send that
    would lose the job outright."""
    queue.enqueue("job-a", delay_s=100_000)
    assert queue.depth().total == 1


def test_receive_batch_is_capped_at_ten(queue: SQSJobQueue):
    for i in range(15):
        queue.enqueue(f"job-{i}")
    assert len(queue.receive(max_messages=50)) == 10


def test_depth_counts_delayed_messages_as_owned_by_nobody(queue: SQSJobQueue):
    """A delayed message is invisible but held by no one. Counting it keeps
    `total` honest, which is what the drain check reads."""
    queue.enqueue("job-a", delay_s=60)
    depth = queue.depth()
    assert depth.visible == 0
    assert depth.total == 1


# --------------------------------------------------------------------------- #
# SQS redrive
# --------------------------------------------------------------------------- #
def test_redrive_happens_without_the_client_asking(queue: SQSJobQueue):
    """The service moves the message; the adapter only reports it."""
    queue.enqueue("job-poison")
    for _ in range(5):
        queue.nack(queue.receive(1)[0])
    assert queue.receive(1) == []
    assert queue.dead_letters() == ["job-poison"]


def test_sweep_reports_each_dead_letter_once(queue: SQSJobQueue):
    """SQS has no 'what changed' API and reading the DLQ does not consume it,
    so the de-duplication has to live on the client side."""
    queue.enqueue("job-poison")
    for _ in range(5):
        queue.nack(queue.receive(1)[0])
    queue.receive(1)

    assert queue.sweep_expired() == ["job-poison"]
    assert queue.sweep_expired() == []


def test_peeking_the_dlq_does_not_consume_it(queue: SQSJobQueue):
    queue.enqueue("job-poison")
    for _ in range(5):
        queue.nack(queue.receive(1)[0])
    queue.receive(1)

    assert queue.dead_letters() == ["job-poison"]
    assert queue.dead_letters() == ["job-poison"], "reading the DLQ consumed it"


def test_no_dlq_configured_means_no_dead_letters_reported(sqs: FakeSQS):
    queue = SQSJobQueue(QUEUE_URL, dlq_url=None, client=sqs)
    assert queue.dead_letters() == []
    assert queue.sweep_expired() == []


def test_a_queue_url_is_required():
    with pytest.raises(ValueError):
        SQSJobQueue("", client=object())


# --------------------------------------------------------------------------- #
# DynamoDB store
# --------------------------------------------------------------------------- #
@pytest.fixture
def table() -> FakeTable:
    return FakeTable()


@pytest.fixture
def store(table: FakeTable) -> DynamoDBStateStore:
    return DynamoDBStateStore("nimbus-state", table=table)


def test_put_and_get_a_job(store: DynamoDBStateStore):
    job = make_job()
    store.put_job(job)
    fetched = store.get_job(job.job_id)
    assert fetched is not None
    assert fetched.job_id == job.job_id
    assert fetched.job_type == "prime_count"
    assert fetched.payload == {"limit": 100_000}


def test_get_of_a_missing_job_is_none(store: DynamoDBStateStore):
    assert store.get_job("job-nope") is None


def test_submitting_the_same_job_twice_fails_the_condition(store: DynamoDBStateStore):
    job = make_job()
    store.put_job(job)
    with pytest.raises(ConditionFailed):
        store.put_job(job)


def test_a_conditional_update_on_the_wrong_status_fails(store: DynamoDBStateStore):
    """The store-side idempotency guard (docs/architecture.md section 8).

    Exactly the semantics LocalStateStore has, which is why the engine needs no
    branch: a late result from a superseded attempt loses this race and is
    discarded rather than overwriting the attempt that replaced it.
    """
    job = make_job()
    store.put_job(job)
    running = job.model_copy(update={"status": JobStatus.RUNNING})
    store.update_job(running, expect_status=JobStatus.QUEUED)

    with pytest.raises(ConditionFailed):
        store.update_job(running, expect_status=JobStatus.QUEUED)


def test_updating_a_missing_job_fails(store: DynamoDBStateStore):
    with pytest.raises(ConditionFailed):
        store.update_job(make_job(), expect_status=JobStatus.QUEUED)


def test_floats_survive_the_decimal_round_trip(store: DynamoDBStateStore):
    """DynamoDB has no float type and the SDK rejects them outright.

    Going through str() rather than Decimal(float) matters: Decimal(0.1) is a
    fifty-digit binary expansion that DynamoDB rejects for exceeding its 38
    digits of precision.
    """
    job = make_job(timeout_s=0.1).model_copy(update={"execution_time_ms": 1234.5678})
    store.put_job(job)
    fetched = store.get_job(job.job_id)
    assert fetched.timeout_s == pytest.approx(0.1)
    assert fetched.execution_time_ms == pytest.approx(1234.5678)


def test_listing_by_status_uses_the_index_newest_first(store: DynamoDBStateStore):
    ids = []
    for _ in range(5):
        job = make_job()
        store.put_job(job)
        ids.append(job.job_id)

    listed = store.list_jobs(status=JobStatus.QUEUED, limit=10)
    assert len(listed) == 5
    assert {j.job_id for j in listed} == set(ids)
    # Descending on {created_at}#{job_id}.
    assert listed == sorted(listed, key=lambda j: (j.created_at, j.job_id), reverse=True)


def test_a_status_listing_respects_its_limit(store: DynamoDBStateStore):
    for _ in range(10):
        store.put_job(make_job())
    assert len(store.list_jobs(status=JobStatus.QUEUED, limit=3)) == 3


def test_listing_by_run_id(store: DynamoDBStateStore):
    for _ in range(3):
        store.put_job(make_job(run_id="run-1"))
    store.put_job(make_job(run_id="run-2"))

    assert len(store.list_jobs(run_id="run-1", limit=100)) == 3
    assert len(store.list_jobs(run_id="run-2", limit=100)) == 1


def test_listing_everything_is_refused_rather_than_scanning(store: DynamoDBStateStore):
    """The access patterns were designed so nothing needs a scan, and the IAM
    policy does not grant dynamodb:Scan. Failing here is how that stays true
    when someone adds a 'just list everything' endpoint in a hurry."""
    with pytest.raises(ValueError, match="scan"):
        store.list_jobs(limit=10)


def test_counts_by_status_covers_every_status(store: DynamoDBStateStore):
    store.put_job(make_job())
    counts = store.counts_by_status()
    assert set(counts) == {s.value for s in JobStatus}
    assert counts["QUEUED"] == 1
    assert counts["COMPLETED"] == 0


def test_a_table_name_is_required():
    with pytest.raises(ValueError):
        DynamoDBStateStore("", table=object())


def test_key_and_index_attributes_never_reach_the_model(store: DynamoDBStateStore, table):
    job = make_job()
    store.put_job(job)
    stored = table.items[(f"JOB#{job.job_id}", "META")]
    assert stored["GSI1PK"] == "STATUS#QUEUED"
    # ...and none of it comes back out.
    assert not hasattr(store.get_job(job.job_id), "GSI1PK")


def test_a_run_reference_item_is_written_for_bulk_listing(store: DynamoDBStateStore, table):
    job = make_job(run_id="run-7")
    store.put_job(job)
    assert (f"RUN#run-7", f"JOB#{job.job_id}") in table.items


# --------------------------------------------------------------------------- #
# S3 artifacts
# --------------------------------------------------------------------------- #
@pytest.fixture
def s3() -> FakeS3:
    return FakeS3()


@pytest.fixture
def artifacts(s3: FakeS3) -> S3ArtifactStore:
    return S3ArtifactStore("nimbus-artifacts", client=s3)


def test_put_and_get_bytes(artifacts: S3ArtifactStore):
    artifacts.put_bytes("jobs/job-1/attempt-000/output.bin", b"result")
    assert artifacts.get_bytes("jobs/job-1/attempt-000/output.bin") == b"result"


def test_put_json_round_trips(artifacts: S3ArtifactStore):
    artifacts.put_json("benchmarks/run-1/manifest.json", {"seed": 1337})
    import json

    assert json.loads(artifacts.get_bytes("benchmarks/run-1/manifest.json")) == {"seed": 1337}


def test_every_object_is_written_encrypted(artifacts: S3ArtifactStore, s3: FakeS3):
    """The bucket policy denies unencrypted puts too. This is the half that
    still holds if the policy is ever loosened."""
    artifacts.put_bytes("k", b"x")
    assert s3.encryption[("nimbus-artifacts", "k")] == "AES256"


def test_exists_distinguishes_a_404_from_a_real_error(artifacts: S3ArtifactStore):
    assert artifacts.exists("nope") is False
    artifacts.put_bytes("yes", b"x")
    assert artifacts.exists("yes") is True


def test_keys_cannot_escape_the_prefix(s3: FakeS3):
    store = S3ArtifactStore("nimbus-artifacts", prefix="runs", client=s3)
    store.put_bytes("../../etc/passwd", b"x")
    assert all(key.startswith("runs/") for _, key in s3.objects)


def test_location_is_an_s3_uri(artifacts: S3ArtifactStore):
    assert artifacts.location("a/b") == "s3://nimbus-artifacts/a/b"


def test_a_bucket_is_required():
    with pytest.raises(ValueError):
        S3ArtifactStore("", client=object())


def test_the_key_layout_matches_the_local_store(artifacts: S3ArtifactStore, tmp_path):
    """A benchmark run directory has the same shape in both backends, so the
    analysis scripts do not need to know which one produced it."""
    from backend.services.artifacts.local import LocalArtifactStore

    local = LocalArtifactStore(str(tmp_path))
    key = "benchmarks/run-1/decisions.jsonl"
    local.put_bytes(key, b"{}")
    artifacts.put_bytes(key, b"{}")

    assert local.location(key).endswith(key)
    assert artifacts.location(key).endswith(key)
