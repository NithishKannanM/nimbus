"""Ordering policies. These see jobs only -- never workers, never telemetry."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Sequence

from backend.models.job import Job
from scheduler.base import OrderingPolicy


def effective_priority(job: Job, now: datetime, aging_interval_s: float) -> int:
    """Priority with aging. 1 = Critical .. 4 = Low; lower is more urgent.

    A job is promoted one level per ``aging_interval_s`` of waiting, bounding
    starvation at ``(priority - 1) * aging_interval_s``.
    """
    if aging_interval_s <= 0:
        return job.priority
    promotions = math.floor(job.wait_seconds(now) / aging_interval_s)
    return max(1, job.priority - promotions)


class FIFOOrdering(OrderingPolicy):
    """Strict arrival order. The baseline, and Phase 2's constant.

    Every Phase 2 strategy uses this ordering, so any measured difference
    between strategies is attributable to placement alone.
    """

    name = "fifo"

    def select_job(self, jobs: Sequence[Job], now: datetime) -> Job | None:
        if not jobs:
            return None
        return min(jobs, key=lambda j: (j.created_at, j.job_id))
