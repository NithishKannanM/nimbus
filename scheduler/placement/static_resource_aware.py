"""Static Resource-Aware placement.

The controlled baseline for the project's central experiment. It reasons about
DECLARED capacity and Nimbus's own RESERVATIONS, and it is structurally
incapable of reading OBSERVED telemetry (see scheduler/views.py).

Formula, frozen before any experiment runs
------------------------------------------

For an eligible worker ``w`` and job ``j``::

    projected_cpu(w, j)   = (cpu_reserved(w)    + cpu_units(j))  / cpu_capacity(w)
    projected_mem(w, j)   = (memory_reserved(w) + memory_mb(j))  / memory_capacity(w)
    projected_slots(w)    = (slots_used(w)      + 1)             / slots_total(w)

    StaticScore(w, j) = W_CPU * projected_cpu
                      + W_MEM * projected_mem
                      + W_SLOTS * projected_slots

Weights sum to 1, every term is in [0, 1], so ``StaticScore`` is in [0, 1] and
is comparable across jobs, workers and runs.

    W_CPU   = 0.40   most contended declared resource, and the finest-grained:
                     job cpu_units range 128..1024 across the workload registry
    W_MEM   = 0.30   coarser, but it is the hard constraint that actually fails
    W_SLOTS = 0.30   the only term identical across job types, which keeps the
                     score meaningful when declared requirements are uniform

Objective: why "balance" and not "binpack"
------------------------------------------

Minimising this score picks the *least projected-loaded* feasible worker --
spreading. The obvious alternative is ECS-style ``binpack``: minimise leftover
capacity, i.e. pick the *most* loaded worker that still fits.

The default is ``balance``, deliberately. The Phase 3 comparison is

    Static Resource-Aware  vs  Telemetry-Aware Adaptive

and Adaptive's whole thesis is "steer away from loaded workers". If the static
baseline packed while Adaptive spread, the experiment would confound two
variables -- the *objective* (pack vs spread) and the *information source*
(declared vs observed) -- and any difference would be uninterpretable. Holding
the objective constant makes the information source the only variable, which is
the entire point.

``binpack`` remains available as a non-default mode because it is the faithful
Amazon ECS analogue and docs/aws-native-scheduling.md compares against it. It is
a separate, labelled configuration, never a silent default swap.

Weights and modes are fixed here in source and must not be tuned against
benchmark results (docs/architecture.md risk R8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Sequence

from scheduler.base import PlacementOutcome, StaticPlacementPolicy, partition_candidates
from scheduler.views import JobRequirements, StaticWorkerView

W_CPU = 0.40
W_MEM = 0.30
W_SLOTS = 0.30

STATIC_WEIGHTS: dict[str, float] = {"cpu": W_CPU, "memory": W_MEM, "slots": W_SLOTS}

Mode = Literal["balance", "binpack"]


#: Mode -> policy name. The name has to identify the *behaviour*, not just the
#: class: both modes share an implementation but make opposite choices, and a
#: trace that recorded only the class name would replay a binpack decision as a
#: balance decision and silently disagree with itself.
MODE_NAMES: dict[str, str] = {
    "balance": "static_resource_aware",
    "binpack": "static_resource_aware_binpack",
}


class StaticResourceAwarePlacement(StaticPlacementPolicy):
    name = "static_resource_aware"
    algorithm_version = "static-v1"

    def __init__(self, *, mode: Mode = "balance") -> None:
        if mode not in MODE_NAMES:
            raise ValueError(
                f"unknown mode {mode!r}; expected one of {', '.join(sorted(MODE_NAMES))}"
            )
        self.mode: Mode = mode
        self.name = MODE_NAMES[mode]

    # -- scoring ---------------------------------------------------------
    @staticmethod
    def score_terms(job: JobRequirements, worker: StaticWorkerView) -> dict[str, float]:
        return {
            "projected_cpu": worker.projected_cpu_utilisation(job.cpu_units),
            "projected_memory": worker.projected_memory_utilisation(job.memory_mb),
            "projected_slots": worker.projected_slot_utilisation(),
        }

    @staticmethod
    def score(terms: dict[str, float]) -> float:
        return (
            W_CPU * terms["projected_cpu"]
            + W_MEM * terms["projected_memory"]
            + W_SLOTS * terms["projected_slots"]
        )

    # -- placement -------------------------------------------------------
    def select_worker(
        self, job: JobRequirements, workers: Sequence[StaticWorkerView], now: datetime
    ) -> PlacementOutcome:
        eligible, excluded = partition_candidates(job, workers)
        if not eligible:
            return PlacementOutcome(
                selected_worker=None,
                reason=f"no eligible worker ({len(excluded)} excluded)",
                excluded=excluded,
                weights=dict(STATIC_WEIGHTS),
            )

        terms: dict[str, dict[str, float]] = {}
        scores: dict[str, float] = {}
        for worker in eligible:
            worker_terms = self.score_terms(job, worker)
            worker_score = self.score(worker_terms)
            worker_terms["contrib_cpu"] = W_CPU * worker_terms["projected_cpu"]
            worker_terms["contrib_memory"] = W_MEM * worker_terms["projected_memory"]
            worker_terms["contrib_slots"] = W_SLOTS * worker_terms["projected_slots"]
            terms[worker.worker_id] = worker_terms
            scores[worker.worker_id] = worker_score

        # Deterministic: primary key is the score (negated for binpack), and
        # ties always break on the lexicographically smallest worker id.
        sign = 1.0 if self.mode == "balance" else -1.0
        selected = min(eligible, key=lambda w: (sign * scores[w.worker_id], w.worker_id))

        objective = "lowest" if self.mode == "balance" else "highest"
        return PlacementOutcome(
            selected_worker=selected.worker_id,
            reason=(
                f"{objective} static resource score ({self.mode}): "
                f"{selected.worker_id}={scores[selected.worker_id]:.4f} "
                f"[cpu {selected.cpu_reserved}+{job.cpu_units}/{selected.cpu_capacity}, "
                f"mem {selected.memory_reserved}+{job.memory_mb}/{selected.memory_capacity}, "
                f"slots {selected.slots_used}+1/{selected.slots_total}]; "
                f"{len(eligible)} eligible, {len(excluded)} excluded"
            ),
            scores=scores,
            terms=terms,
            excluded=excluded,
            weights=dict(STATIC_WEIGHTS),
            objective="min" if self.mode == "balance" else "max",
        )
