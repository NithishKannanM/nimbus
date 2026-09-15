"""Strategy registry.

A strategy is a named (ordering, placement) pair. Every Phase 2 strategy uses
FIFO ordering, so a comparison between them isolates placement.

Only strategies that are actually implemented appear here. ``adaptive`` is
Phase 3 and must keep returning an accurate error until it exists.
"""

from __future__ import annotations

from typing import Callable

from scheduler.base import OrderingPolicy, PlacementPolicy, StaticPlacementPolicy
from scheduler.ordering import FIFOOrdering
from scheduler.placement import (
    AdaptivePlacement,
    RoundRobinPlacement,
    StaticResourceAwarePlacement,
)
from scheduler.strategy import SchedulingStrategy


#: Every factory takes the same parameters, so adding a strategy never requires
#: special-casing inside ``build_scheduler``.
StrategyFactory = Callable[[float, float], SchedulingStrategy]


def _compose(name: str, placement, aging_interval_s: float) -> SchedulingStrategy:
    """All strategies share FIFO ordering.

    That is the experimental control: a comparison between two strategies
    isolates *placement*, because ordering is held constant. The Phase 1 design
    sketched adaptive with priority ordering; it was dropped for exactly this
    reason.
    """
    return SchedulingStrategy(name, FIFOOrdering(aging_interval_s=aging_interval_s), placement)


def _fifo(aging_interval_s: float, _max_age_s: float) -> SchedulingStrategy:
    return _compose("fifo", RoundRobinPlacement(), aging_interval_s)


def _static_resource_aware(aging_interval_s: float, _max_age_s: float) -> SchedulingStrategy:
    return _compose(
        "static_resource_aware", StaticResourceAwarePlacement(mode="balance"), aging_interval_s
    )


def _static_resource_aware_binpack(
    aging_interval_s: float, _max_age_s: float
) -> SchedulingStrategy:
    """The Amazon ECS ``binpack`` analogue. A separate labelled configuration,
    never a silent swap of the default objective."""
    return _compose(
        "static_resource_aware_binpack",
        StaticResourceAwarePlacement(mode="binpack"),
        aging_interval_s,
    )


def _adaptive(aging_interval_s: float, telemetry_max_age_s: float) -> SchedulingStrategy:
    """adaptive-v1 -- the only policy that consumes OBSERVED state."""
    return _compose(
        "adaptive", AdaptivePlacement(telemetry_max_age_s=telemetry_max_age_s), aging_interval_s
    )


STRATEGIES: dict[str, StrategyFactory] = {
    "fifo": _fifo,
    "static_resource_aware": _static_resource_aware,
    "static_resource_aware_binpack": _static_resource_aware_binpack,
    "adaptive": _adaptive,
}


def build_scheduler(
    name: str,
    *,
    aging_interval_s: float = 30.0,
    telemetry_enabled: bool = False,
    telemetry_max_age_s: float = 6.0,
) -> SchedulingStrategy:
    try:
        factory = STRATEGIES[name]
    except KeyError:
        raise ValueError(
            f"unknown strategy {name!r}; available: {', '.join(sorted(STRATEGIES))}"
        ) from None
    strategy = factory(aging_interval_s, telemetry_max_age_s)
    strategy.telemetry_enabled = telemetry_enabled
    strategy.telemetry_max_age_s = telemetry_max_age_s
    return strategy


__all__ = [
    "AdaptivePlacement",
    "OrderingPolicy",
    "PlacementPolicy",
    "StaticPlacementPolicy",
    "SchedulingStrategy",
    "FIFOOrdering",
    "RoundRobinPlacement",
    "StaticResourceAwarePlacement",
    "STRATEGIES",
    "build_scheduler",
]
