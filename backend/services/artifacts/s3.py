"""S3-backed artifact store.

``LocalArtifactStore`` was written to mirror this key layout exactly, so a
benchmark run directory has the same shape in both backends and the analysis
scripts do not care which one produced it:

    inputs/{job_id}/input.bin
    jobs/{job_id}/attempt-{n}/output.bin
    benchmarks/{run_id}/manifest.json
    benchmarks/{run_id}/raw.jsonl
    benchmarks/{run_id}/decisions.jsonl
    benchmarks/{run_id}/telemetry.jsonl

Keys are deterministic and include the attempt number, which is what makes
re-execution idempotent at the storage layer (docs/architecture.md section 8,
guard 4): a retried attempt writes to its own key, and a *repeated* attempt
overwrites its own previous output in place rather than accumulating duplicates
nobody can tell apart.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("nimbus.artifacts.s3")


class S3ArtifactStore:
    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        region: str | None = None,
        client: Any = None,
    ) -> None:
        if not bucket:
            raise ValueError("bucket is required for the AWS backend")
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        if client is None:
            from backend.aws.clients import client as make_client  # noqa: PLC0415

            client = make_client("s3", region=region)
        self._s3 = client

    def _key(self, key: str) -> str:
        # Keys are control-plane generated, but normalise anyway. The local
        # store does the same thing for the same reason: an artifact key must
        # never be able to escape its namespace, and "our own code generates
        # it" is an argument that stops being true the moment someone adds an
        # endpoint that takes one.
        clean = os.path.normpath("/" + key).lstrip("/")
        return f"{self._prefix}/{clean}" if self._prefix else clean

    def put_bytes(self, key: str, data: bytes) -> str:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._key(key),
            Body=data,
            # Belt and braces: the bucket enforces SSE-S3 by policy in
            # infra/s3.tf, and the request asks for it too. If the policy is
            # ever loosened, objects written by Nimbus stay encrypted.
            ServerSideEncryption="AES256",
        )
        return self.location(key)

    def get_bytes(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self._bucket, Key=self._key(key))["Body"].read()

    def put_json(self, key: str, obj: Any) -> str:
        return self.put_bytes(key, json.dumps(obj, indent=2, default=str).encode())

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._key(key))
            return True
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def location(self, key: str) -> str:
        return f"s3://{self._bucket}/{self._key(key)}"
