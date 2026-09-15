"""CPU-bound workloads. Each returns a checksum so a caller can verify that
real computation happened and that repeated runs agree."""

from __future__ import annotations

import hashlib

import numpy as np


def matrix_multiply(size: int, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    a = rng.random((size, size), dtype=np.float64)
    b = rng.random((size, size), dtype=np.float64)
    c = a @ b
    return {
        "size": size,
        "flops_approx": 2 * size**3,
        "checksum": float(np.round(c.trace(), 6)),
    }


def prime_count(limit: int) -> dict:
    """Sieve of Eratosthenes over a bytearray.

    Not vectorised through numpy, but the hot loop is C-level slice assignment,
    so this is a memory-traversal workload more than an interpreter-bound one.
    ``hash_chain`` is the workload that burns a core in Python bytecode, and
    ``matrix_multiply`` is the one that exercises BLAS -- the three together
    cover meaningfully different shapes of "CPU-bound".
    """
    sieve = bytearray([1]) * (limit + 1)
    sieve[0:2] = b"\x00\x00"
    n = 2
    while n * n <= limit:
        if sieve[n]:
            sieve[n * n :: n] = bytearray(len(range(n * n, limit + 1, n)))
        n += 1
    count = sum(sieve)
    largest = limit
    while largest > 1 and not sieve[largest]:
        largest -= 1
    return {"limit": limit, "primes": count, "largest_prime": largest, "checksum": count}


def hash_chain(rounds: int, seed: int = 0) -> dict:
    digest = hashlib.sha256(str(seed).encode()).digest()
    for _ in range(rounds):
        digest = hashlib.sha256(digest).digest()
    return {"rounds": rounds, "checksum": digest.hex()[:16]}
