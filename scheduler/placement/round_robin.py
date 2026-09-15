"""Round-robin placement -- the Phase 1 baseline, preserved exactly.

Round-robin rather than "first worker with a free slot": "first free"
degenerates to always-worker-1 under light load, which would make the baseline
an unrealistically bad straw man. A baseline has to be a reasonable baseline or
beating it proves nothing.

Note what this policy does *not* look at: not declared capacity, not
reservations beyond slot occupancy, and certainly not telemetry. It rotates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from scheduler.base import PlacementOutcome, StaticPlacementPolicy, partition_candidates
from scheduler.views import JobRequirements, StaticWorkerView


class RoundRobinPlacement(StaticPlacementPolicy):
    name = "round_robin"
    algorithm_version = "round-robin-v1"

    def __init__(self) -> None:
        self._cursor = 0

    def select_worker(
        self, job: JobRequirements, workers: Sequence[StaticWorkerView], now: datetime
    ) -> PlacementOutcome:
        eligible, excluded = partition_candidates(job, workers)
        if not eligible:
            return PlacementOutcome(
                selected_worker=None,
                reason=f"no eligible worker ({len(excluded)} excluded)",
                excluded=excluded,
            )

        ordered = sorted(eligible, key=lambda w: w.worker_id)
        index = self._cursor % len(ordered)
        selected = ordered[index]
        self._cursor = index + 1

        # Score is distance from the rotation cursor, so the trace explains the
        # choice in the same shape the scored policies use.
        scores = {w.worker_id: float((i - index) % len(ordered)) for i, w in enumerate(ordered)}
        terms = {
            w.worker_id: {
                "rotation_offset": scores[w.worker_id],
                "slots_used": float(w.slots_used),
                "slots_total": float(w.slots_total),
            }
            for w in ordered
        }
        return PlacementOutcome(
            selected_worker=selected.worker_id,
            reason=(
                f"round-robin slot on {selected.worker_id} "
                f"({selected.slots_used}/{selected.slots_total} slots busy, "
                f"{len(ordered)} eligible)"
            ),
            scores=scores,
            terms=terms,
            excluded=excluded,
        )
