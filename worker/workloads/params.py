"""Payload schemas for every job type.

Bounds are load-bearing security, not decoration: a job payload is untrusted
input that controls how much CPU and memory a worker allocates
(docs/limitations.md section 5). Job types are a closed registry -- payloads
never name code to execute.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WorkloadParams(BaseModel):
    """Base for every payload schema.

    ``extra="forbid"`` is deliberate: an unrecognised payload key is a
    caller error worth surfacing, not something to silently drop.
    """

    model_config = ConfigDict(extra="forbid")


class MatrixMultiplyParams(WorkloadParams):
    size: int = Field(400, ge=16, le=2500)
    seed: int = 0


class PrimeCountParams(WorkloadParams):
    limit: int = Field(200_000, ge=1_000, le=20_000_000)


class HashChainParams(WorkloadParams):
    rounds: int = Field(200_000, ge=1_000, le=20_000_000)
    seed: int = 0


class SortLargeParams(WorkloadParams):
    elements: int = Field(2_000_000, ge=10_000, le=40_000_000)
    seed: int = 0


class ArrayTransformParams(WorkloadParams):
    mb: int = Field(128, ge=1, le=1024)
    passes: int = Field(3, ge=1, le=20)


class CompressFileParams(WorkloadParams):
    mb: int = Field(32, ge=1, le=512)
    level: int = Field(6, ge=1, le=9)
    seed: int = 0


class FileScanParams(WorkloadParams):
    mb: int = Field(64, ge=1, le=512)
    chunk_kb: int = Field(64, ge=4, le=8192)
    seed: int = 0


class JsonTransformParams(WorkloadParams):
    records: int = Field(1_000, ge=10, le=200_000)
    seed: int = 0


class MetadataExtractParams(WorkloadParams):
    count: int = Field(50, ge=1, le=5_000)
