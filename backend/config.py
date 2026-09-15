"""Single source of truth for Nimbus configuration.

Every tunable lives here so that a benchmark manifest can serialise the exact
configuration a run used (docs/experiments.md section 2.4).
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NimbusConfig(BaseSettings):
    # populate_by_name is load-bearing: telemetry_interval_s carries a
    # validation_alias, and without this a plain
    # ``NimbusConfig(telemetry_interval_s=0.5)`` would be silently ignored
    # and fall back to the default -- which would quietly invalidate any
    # experiment that varied the sampling interval.
    model_config = SettingsConfigDict(
        env_prefix="NIMBUS_", env_file=".env", extra="ignore", populate_by_name=True
    )

    # --- backend selection -------------------------------------------------
    backend: str = "local"  # local | aws  (aws arrives in Phase 5)

    # --- scheduling --------------------------------------------------------
    strategy: str = "fifo"
    scheduler_tick_s: float = 0.02
    aging_interval_s: float = 30.0

    # --- fleet -------------------------------------------------------------
    worker_count: int = 3
    worker_slots: int = 2
    # Declared capacity in ECS-style units (1 vCPU = 1024). Kept consistent
    # with worker_slots: a 2-slot worker that declares only 1024 units
    # cannot host two CPU_BOUND jobs, and half its slots are unusable.
    worker_cpu_units: int = 2048
    worker_memory_mb: int = 4096

    # --- telemetry / health ------------------------------------------------
    # Telemetry is OBSERVED state. It is collected, stored and exposed, and is
    # deliberately not readable by any placement policy implemented so far
    # (see scheduler/views.py). Toggling it must not change placement -- that
    # is the Phase 2 control experiment.
    telemetry_enabled: bool = True
    telemetry_interval_s: float = Field(
        2.0,
        validation_alias=AliasChoices(
            "NIMBUS_TELEMETRY_INTERVAL_S", "NIMBUS_TELEMETRY_INTERVAL_SECONDS"
        ),
    )
    telemetry_history: int = 300
    #: Samples in the smoothing window. adaptive-v1 pre-registered this at 3;
    #: changing it changes the algorithm and requires a new version identity.
    telemetry_window: int = 3
    #: Telemetry older than this is stale. Default = 3 sampling intervals at the
    #: default 2 s interval (docs/adaptive-v1-preregistration.md section 3).
    telemetry_max_age_s: float = Field(
        6.0,
        validation_alias=AliasChoices(
            "NIMBUS_TELEMETRY_MAX_AGE_S", "NIMBUS_TELEMETRY_MAX_AGE_SECONDS"
        ),
    )
    heartbeat_interval_s: float = 5.0
    heartbeat_timeout_s: float = 15.0
    eviction_timeout_s: float = 60.0

    # --- reliability -------------------------------------------------------
    job_timeout_s: float = 300.0
    max_attempts: int = 3
    visibility_timeout_s: float = 60.0

    #: How often the reaper sweeps for jobs nobody will ever report on.
    #: Bounds detection latency for a lost worker, so it must stay well below
    #: heartbeat_timeout_s to be worth anything.
    reaper_interval_s: float = 2.0
    #: A job in SCHEDULED/RUNNING with no lease is only "orphaned" once it has
    #: been that way longer than any legitimate gap between control-plane
    #: steps. Too low and the reaper reclaims jobs that are merely in transit.
    orphan_grace_s: float = 30.0

    #: Retry backoff. Jitter is deterministic in (job_id, attempt) so that two
    #: runs of the same seeded profile do not diverge on retry timing --
    #: see backend/services/retry.py.
    retry_base_s: float = 0.5
    retry_cap_s: float = 30.0
    retry_jitter_ratio: float = 0.25

    # --- storage -----------------------------------------------------------
    artifact_dir: str = ".nimbus/artifacts"
    inline_payload_limit_bytes: int = 256 * 1024

    # --- aws (backend="aws" only) ------------------------------------------
    # Resource *addresses* only. Credentials are never read from configuration:
    # on EC2 they come from the instance profile and locally from the standard
    # boto3 chain, so there is no path by which a key could reach a config
    # file, a log line or a benchmark manifest (docs/limitations.md section 5).
    aws_region: str = ""
    sqs_queue_url: str = ""
    sqs_dlq_url: str = ""
    dynamodb_table: str = "nimbus-state"
    s3_bucket: str = ""

    # --- experiment --------------------------------------------------------
    seed: int = 1337

    # --- api ---------------------------------------------------------------
    enable_admin: bool = False


_config: NimbusConfig | None = None


def get_config() -> NimbusConfig:
    global _config
    if _config is None:
        _config = NimbusConfig()
    return _config


def set_config(config: NimbusConfig) -> None:
    """Test hook: install an explicit configuration."""
    global _config
    _config = config
