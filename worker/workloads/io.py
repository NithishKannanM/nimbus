"""I/O-bound workloads. They use a caller-supplied scratch directory so a
worker's filesystem traffic is isolated and cleanable."""

from __future__ import annotations

import gzip
import hashlib
import os
import tempfile

import numpy as np

_CHUNK = 1 << 20
_TOKEN_BYTES = 256
_TOKENS = 512


def _write_blob(path: str, mb: int, seed: int) -> None:
    """Write ``mb`` MiB of seeded, partially-compressible bytes.

    Assembled from a fixed table of random 256-byte tokens. Pure random bytes
    would make gzip a no-op (ratio ~1.0) and a repeating pattern would make it
    trivial (ratio ~0.02); sampling with replacement from a small token table
    lands in the realistic middle, so compression level actually costs
    something.
    """
    rng = np.random.default_rng(seed)
    tokens = rng.bytes(_TOKENS * _TOKEN_BYTES)
    table = np.frombuffer(tokens, dtype=np.uint8).reshape(_TOKENS, _TOKEN_BYTES)
    per_chunk = _CHUNK // _TOKEN_BYTES
    with open(path, "wb") as fh:
        for _ in range(mb):
            idx = rng.integers(0, _TOKENS, size=per_chunk)
            fh.write(table[idx].tobytes())
        fh.flush()
        os.fsync(fh.fileno())


def compress_file(mb: int, level: int = 6, seed: int = 0, scratch: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(dir=scratch, prefix="nimbus-io-") as tmp:
        src = os.path.join(tmp, "input.bin")
        dst = os.path.join(tmp, "input.bin.gz")
        _write_blob(src, mb, seed)
        raw = os.path.getsize(src)
        with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=level) as fout:
            while chunk := fin.read(_CHUNK):
                fout.write(chunk)
        packed = os.path.getsize(dst)
        return {
            "mb": mb,
            "level": level,
            "raw_bytes": raw,
            "compressed_bytes": packed,
            "ratio": round(packed / raw, 4) if raw else 0.0,
            "checksum": packed,
        }


def file_scan(mb: int, chunk_kb: int = 64, seed: int = 0, scratch: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(dir=scratch, prefix="nimbus-io-") as tmp:
        src = os.path.join(tmp, "scan.bin")
        _write_blob(src, mb, seed)
        digest = hashlib.sha256()
        reads = 0
        with open(src, "rb") as fh:
            while chunk := fh.read(chunk_kb * 1024):
                digest.update(chunk)
                reads += 1
        return {
            "mb": mb,
            "chunk_kb": chunk_kb,
            "reads": reads,
            "checksum": digest.hexdigest()[:16],
        }
