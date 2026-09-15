"""Guards on the telemetry boundary itself.

If these fail, the Phase 3 adaptive-vs-static comparison has lost its control,
so they are deliberately strict: they pin the exact field set of the static
view rather than merely checking current behaviour.
"""

from __future__ import annotations

import dataclasses

import pytest

from backend.models.enums import WorkerStatus
from backend.models.workload_helpers import make_job
from scheduler.views import (
    OBSERVED_FIELD_TOKENS,
    STATIC_VIEW_FIELDS,
    JobRequirements,
    StaticWorkerView,
    job_requirements,
    static_fleet_view,
    static_view,
)
from tests.conftest import telemetry, worker_state


def test_static_view_field_set_is_pinned():
    """Adding a field to StaticWorkerView must be a deliberate act that updates
    this test, not something that slips in with a feature."""
    assert STATIC_VIEW_FIELDS == {
        "worker_id",
        "healthy",
        "cpu_capacity",
        "memory_capacity",
        "cpu_reserved",
        "memory_reserved",
        "slots_total",
        "slots_used",
    }


def test_no_static_view_field_names_like_telemetry():
    for name in STATIC_VIEW_FIELDS:
        for token in OBSERVED_FIELD_TOKENS:
            assert token not in name, f"{name!r} looks like observed state"


def test_static_view_has_no_telemetry_attributes():
    view = static_view(worker_state("worker-1", observed=telemetry("worker-1", cpu=99.0)))
    for attribute in (
        "observed", "cpu_percent", "memory_percent", "load_average_1m",
        "available_memory_mb", "process_memory_mb", "cpu_trend",
    ):
        assert not hasattr(view, attribute)
        with pytest.raises(AttributeError):
            getattr(view, attribute)


def test_telemetry_cannot_be_attached_to_a_static_view():
    """slots=True removes __dict__, so there is nowhere to smuggle it."""
    view = static_view(worker_state("worker-1"))
    with pytest.raises(AttributeError):
        object.__setattr__(view, "cpu_percent", 99.0)


def test_static_view_is_immutable():
    view = static_view(worker_state("worker-1"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.cpu_reserved = 0  # type: ignore[misc]


def test_projection_carries_declared_and_reserved_faithfully():
    state = worker_state(
        "worker-2", slots=4, cpu_capacity=4096, memory_capacity=8192,
        cpu_reserved=1024, memory_reserved=2048, active_jobs=3,
    )
    view = static_view(state)
    assert view.worker_id == "worker-2"
    assert (view.cpu_capacity, view.cpu_reserved, view.cpu_available) == (4096, 1024, 3072)
    assert (view.memory_capacity, view.memory_reserved, view.memory_available) == (8192, 2048, 6144)
    assert (view.slots_total, view.slots_used, view.slots_free) == (4, 3, 1)
    assert view.healthy is True


def test_projection_reports_health_from_status():
    for status, expected in (
        (WorkerStatus.HEALTHY, True),
        (WorkerStatus.UNHEALTHY, False),
        (WorkerStatus.DRAINING, False),
        (WorkerStatus.DEAD, False),
    ):
        assert static_view(worker_state("w", status=status)).healthy is expected


def test_projected_utilisation_maths():
    view = static_view(worker_state("w", slots=2, cpu_capacity=2048, cpu_reserved=512,
                                    memory_capacity=4096, memory_reserved=1024, active_jobs=1))
    assert view.projected_cpu_utilisation(512) == pytest.approx((512 + 512) / 2048)
    assert view.projected_memory_utilisation(1024) == pytest.approx((1024 + 1024) / 4096)
    assert view.projected_slot_utilisation() == pytest.approx((1 + 1) / 2)


def test_zero_capacity_projects_as_fully_utilised():
    """Guards against a divide-by-zero turning a misconfigured worker into the
    most attractive candidate."""
    view = static_view(worker_state("w", cpu_capacity=0, memory_capacity=0, slots=0))
    assert view.projected_cpu_utilisation(100) == 1.0
    assert view.projected_memory_utilisation(100) == 1.0
    assert view.projected_slot_utilisation() == 1.0


def test_fleet_projection_preserves_order():
    states = (worker_state("worker-1"), worker_state("worker-2"), worker_state("worker-3"))
    assert [v.worker_id for v in static_fleet_view(states)] == [
        "worker-1", "worker-2", "worker-3"
    ]


def test_job_requirements_view_exposes_only_declared_needs():
    job = make_job("matrix_multiply", payload={"size": 900}, cpu_units=1024, memory_mb=512)
    req = job_requirements(job)
    assert isinstance(req, JobRequirements)
    assert (req.cpu_units, req.memory_mb, req.priority) == (1024, 512, 3)
    for attribute in ("payload", "result", "execution_time_ms", "assigned_worker"):
        assert not hasattr(req, attribute)


# --------------------------------------------------------------------------- #
# AdaptiveWorkerView -- the Phase 3 information budget
# --------------------------------------------------------------------------- #
def test_adaptive_view_field_set_is_pinned():
    """Adding a field must be a deliberate act that updates this test. In
    particular, no registry handle, mutable worker internal, or history buffer
    may leak into a placement policy's input."""
    from scheduler.views import ADAPTIVE_VIEW_FIELDS

    assert ADAPTIVE_VIEW_FIELDS == {
        "worker_id", "healthy",
        # DECLARED
        "cpu_capacity", "memory_capacity", "slots_total",
        # RESERVED
        "cpu_reserved", "memory_reserved", "slots_used",
        # OBSERVED
        "cpu_percent", "cpu_percent_latest", "cpu_trend_pct_per_s",
        "memory_percent", "available_memory_mb", "process_memory_mb", "load_average",
        # TELEMETRY QUALITY
        "telemetry_age_s", "telemetry_samples", "telemetry_fresh",
    }


def test_adaptive_view_is_a_strict_superset_of_the_static_view():
    """Both must expose the same DECLARED/RESERVED names so a single
    feasibility filter serves both, instead of two copies that can drift."""
    from scheduler.views import ADAPTIVE_VIEW_FIELDS, STATIC_VIEW_FIELDS

    assert STATIC_VIEW_FIELDS <= ADAPTIVE_VIEW_FIELDS


def test_both_views_satisfy_the_static_field_protocol():
    from scheduler.views import HasStaticFields, adaptive_view

    state = worker_state("worker-1", observed=telemetry("worker-1", cpu=50.0))
    assert isinstance(static_view(state), HasStaticFields)
    assert isinstance(adaptive_view(state, max_age_s=6.0), HasStaticFields)


def test_adaptive_view_is_immutable_and_slotted():
    from scheduler.views import adaptive_view

    view = adaptive_view(worker_state("worker-1"), max_age_s=6.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.cpu_percent = 0.0  # type: ignore[misc]
    with pytest.raises(AttributeError):
        object.__setattr__(view, "smuggled", 1)


def test_adaptive_view_exposes_no_worker_internals():
    from scheduler.views import adaptive_view

    view = adaptive_view(worker_state("worker-1"), max_age_s=6.0)
    for attribute in ("declared", "reserved", "observed", "status", "registry", "_record"):
        assert not hasattr(view, attribute)


def test_static_view_still_has_no_telemetry_after_phase_3():
    """Phase 2's guarantee must survive the arrival of a telemetry-aware
    policy: static placement remains structurally unable to see observed state."""
    from scheduler.views import ADAPTIVE_VIEW_FIELDS, STATIC_VIEW_FIELDS

    observed_only = ADAPTIVE_VIEW_FIELDS - STATIC_VIEW_FIELDS
    assert observed_only & STATIC_VIEW_FIELDS == set()
    view = static_view(worker_state("w", observed=telemetry("w", cpu=99.0)))
    for field in observed_only:
        assert not hasattr(view, field)
