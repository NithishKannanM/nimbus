"""Experiment manifests.

Every run records enough to reproduce it. A number without a manifest is not a
result (docs/experiments.md section 2.4).
"""

from __future__ import annotations

import platform
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import psutil


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def host_info() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "total_memory_gb": round(psutil.virtual_memory().total / 1e9, 2),
    }


@dataclass
class ExperimentManifest:
    experiment_id: str = field(default_factory=lambda: f"exp-{uuid.uuid4().hex[:8]}")
    seed: int = 0
    ordering: str = ""
    placement: str = ""
    strategy: str = ""
    #: Placement algorithm identity, e.g. "adaptive-v1". Never overwritten, so a
    #: later formula change cannot contaminate comparisons against this run.
    algorithm_version: str = "unversioned"
    telemetry: bool = False
    telemetry_interval_s: float = 0.0
    workers: int = 0
    worker_slots: int = 0
    worker_cores: dict[str, list[int]] = field(default_factory=dict)
    declared_cpu_units: int = 0
    declared_memory_mb: int = 0
    profile: str = ""
    profile_description: str = ""
    job_count: int = 0
    size_scale: float = 1.0
    pressure: list[dict[str, Any]] = field(default_factory=list)
    pressure_backend: str | None = None
    trial: int = 0
    config: dict[str, Any] = field(default_factory=dict)
    job_plan: list[dict[str, Any]] = field(default_factory=list)
    git_commit: str | None = field(default_factory=git_commit)
    host: dict[str, Any] = field(default_factory=host_info)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
