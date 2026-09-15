"""The backoff policy.

The property that actually matters here is determinism. Nimbus compares
strategies on identical job sequences, so a random jitter drawn from a global
RNG would make two runs of the same seeded profile diverge on retry timing for
reasons unrelated to the scheduler under test.
"""

from __future__ import annotations

import pytest

from backend.services.retry import DEFAULT_BACKOFF, BackoffPolicy


def test_delay_grows_exponentially():
    policy = BackoffPolicy(base_s=1.0, cap_s=1000.0, jitter_ratio=0.0)
    assert policy.delay_for("job-a", 1) == 1.0
    assert policy.delay_for("job-a", 2) == 2.0
    assert policy.delay_for("job-a", 3) == 4.0
    assert policy.delay_for("job-a", 4) == 8.0


def test_delay_is_capped():
    policy = BackoffPolicy(base_s=1.0, cap_s=5.0, jitter_ratio=0.0)
    assert policy.delay_for("job-a", 10) == 5.0
    # An absurd attempt count must not overflow into inf.
    assert policy.delay_for("job-a", 10_000) == 5.0


def test_jitter_never_exceeds_the_cap():
    """Jitter subtracts, so the cap is a genuine upper bound."""
    policy = BackoffPolicy(base_s=1.0, cap_s=4.0, jitter_ratio=0.25)
    for attempt in range(1, 40):
        for job in (f"job-{i:04x}" for i in range(200)):
            delay = policy.delay_for(job, attempt)
            assert 0.0 < delay <= 4.0


def test_jitter_is_deterministic_in_job_and_attempt():
    a = [DEFAULT_BACKOFF.delay_for("job-abc", n) for n in range(1, 6)]
    b = [DEFAULT_BACKOFF.delay_for("job-abc", n) for n in range(1, 6)]
    assert a == b, "the same (job, attempt) must always produce the same delay"


def test_jitter_decorrelates_different_jobs():
    """The point of jitter: a burst of simultaneous failures must not all
    become visible again at the same instant."""
    delays = {DEFAULT_BACKOFF.delay_for(f"job-{i:04x}", 1) for i in range(200)}
    assert len(delays) > 150, "jitter is not spreading retries apart"


def test_jitter_ratio_bounds_the_spread():
    policy = BackoffPolicy(base_s=8.0, cap_s=100.0, jitter_ratio=0.25)
    delays = [policy.delay_for(f"job-{i:04x}", 1) for i in range(500)]
    assert min(delays) >= 8.0 * 0.75
    assert max(delays) <= 8.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_s": 0.0},
        {"base_s": -1.0},
        {"base_s": 10.0, "cap_s": 1.0},
        {"jitter_ratio": 1.0},
        {"jitter_ratio": -0.1},
    ],
)
def test_incoherent_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        BackoffPolicy(**kwargs)


def test_zero_attempts_is_treated_as_the_first_retry():
    policy = BackoffPolicy(base_s=2.0, cap_s=100.0, jitter_ratio=0.0)
    assert policy.delay_for("job-a", 0) == 2.0
