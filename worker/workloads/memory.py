"""Memory-bound workloads: large allocations and full-array traversals."""

from __future__ import annotations

import numpy as np

_BYTES_PER_FLOAT64 = 8


def sort_large(elements: int, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    data = rng.random(elements, dtype=np.float64)
    data.sort(kind="quicksort")
    return {
        "elements": elements,
        "approx_mb": round(elements * _BYTES_PER_FLOAT64 / 1e6, 2),
        "sorted": bool(data[0] <= data[-1]),
        "checksum": float(np.round(data[elements // 2], 9)),
    }


def array_transform(mb: int, passes: int = 3) -> dict:
    elements = int(mb * 1e6 / _BYTES_PER_FLOAT64)
    data = np.linspace(0.0, 1.0, elements, dtype=np.float64)
    for i in range(passes):
        # Out-of-place so each pass touches ~2x the array: this is a memory
        # bandwidth workload, not an arithmetic one.
        data = data * 1.000001 + float(i)
    return {
        "mb": mb,
        "passes": passes,
        "elements": elements,
        "checksum": float(np.round(data.sum() / elements, 6)),
    }
