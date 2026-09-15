from __future__ import annotations

from scheduler.placement.adaptive import (
    ADAPTIVE_WEIGHTS,
    TREND_SCALE,
    AdaptivePlacement,
)
from scheduler.placement.round_robin import RoundRobinPlacement
from scheduler.placement.static_resource_aware import (
    STATIC_WEIGHTS,
    StaticResourceAwarePlacement,
)

__all__ = [
    "RoundRobinPlacement",
    "StaticResourceAwarePlacement",
    "AdaptivePlacement",
    "STATIC_WEIGHTS",
    "ADAPTIVE_WEIGHTS",
    "TREND_SCALE",
]
