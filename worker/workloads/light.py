"""Lightweight workloads: milliseconds of work, dominated by queueing delay."""

from __future__ import annotations

import json
import os
import tempfile


def json_transform(records: int, seed: int = 0) -> dict:
    rows = [
        {
            "id": i,
            "name": f"item-{(i * 7 + seed) % 1000}",
            "value": ((i * 37 + seed) % 997) / 7.0,
            "tags": [f"t{(i + k) % 11}" for k in range(3)],
        }
        for i in range(records)
    ]
    encoded = json.dumps(rows, separators=(",", ":"))
    decoded = json.loads(encoded)
    total = sum(r["value"] for r in decoded)
    by_tag: dict[str, int] = {}
    for row in decoded:
        for tag in row["tags"]:
            by_tag[tag] = by_tag.get(tag, 0) + 1
    return {
        "records": records,
        "bytes": len(encoded),
        "distinct_tags": len(by_tag),
        "checksum": round(total, 4),
    }


def metadata_extract(count: int, scratch: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(dir=scratch, prefix="nimbus-meta-") as tmp:
        total = 0
        for i in range(count):
            path = os.path.join(tmp, f"f{i:05d}.txt")
            payload = f"nimbus-{i}\n" * (i % 17 + 1)
            with open(path, "w") as fh:
                fh.write(payload)
            total += os.stat(path).st_size
        return {"count": count, "total_bytes": total, "checksum": total}
