"""Retry scheduling: how long a failed attempt waits before it is eligible again.

Separated from the reaper because the *policy* (how long to wait) and the
*mechanism* (who notices and requeues) are different concerns, and only the
policy needs to be deterministic.

Determinism matters here more than it usually does. Nimbus compares scheduling
strategies on identical job sequences, so anything that perturbs *when* a job
becomes placeable perturbs the comparison. Random jitter drawn from a global RNG
would make two runs of the same seeded profile diverge for reasons that have
nothing to do with the scheduler under test. The jitter here is therefore a
deterministic function of ``(job_id, attempt)``: it still de-correlates a burst
of simultaneous failures, which is what jitter is for, but replaying the same
run reproduces the same delays.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential backoff with deterministic, bounded jitter.

    ``delay(n) = min(base * 2**(n-1), cap) * (1 - jitter_ratio * u(job_id, n))``

    Jitter subtracts rather than adds so the cap is a genuine upper bound, and
    ``u`` is a uniform-ish value in [0, 1) derived by hashing. A retry never
    waits longer than ``cap``, which keeps a stuck job from silently sitting
    out most of a benchmark run.
    """

    base_s: float = 0.5
    cap_s: float = 30.0
    jitter_ratio: float = 0.25

    def __post_init__(self) -> None:
        if self.base_s <= 0:
            raise ValueError("base_s must be positive")
        if self.cap_s < self.base_s:
            raise ValueError("cap_s must be >= base_s")
        if not 0.0 <= self.jitter_ratio < 1.0:
            raise ValueError("jitter_ratio must be in [0, 1)")

    def delay_for(self, job_id: str, attempt: int) -> float:
        """Seconds to wait before attempt ``attempt + 1`` becomes visible.

        ``attempt`` is the number of attempts already consumed, so the first
        retry (attempt=1) waits ``base_s`` modulo jitter.
        """
        exponent = max(0, attempt - 1)
        # 2**exponent overflows into a float inf for a pathological attempt
        # count; clamp the exponent instead of relying on min() to catch inf.
        exponent = min(exponent, 32)
        raw = min(self.base_s * (2.0**exponent), self.cap_s)
        return round(raw * (1.0 - self.jitter_ratio * self._unit(job_id, attempt)), 6)

    @staticmethod
    def _unit(job_id: str, attempt: int) -> float:
        """A stable value in [0, 1) for this (job, attempt) pair."""
        digest = hashlib.blake2b(
            f"{job_id}#{attempt}".encode(), digest_size=8
        ).digest()
        return int.from_bytes(digest, "big") / float(1 << 64)


#: The default policy. Tuned to the local benchmark's timescale: jobs here run
#: for 0.1-40 s, so a 30 s cap is already long enough to matter and anything
#: larger would dominate a run rather than protect it.
DEFAULT_BACKOFF = BackoffPolicy()
