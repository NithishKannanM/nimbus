from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class LocalArtifactStore:
    """Filesystem store mirroring the S3 key layout exactly, so a benchmark run
    directory has the same shape locally and on AWS."""

    def __init__(self, root: str = ".nimbus/artifacts") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        # Keys are control-plane generated, but normalise anyway: an artifact
        # key must never escape the store root.
        clean = os.path.normpath("/" + key).lstrip("/")
        return self._root / clean

    def put_bytes(self, key: str, data: bytes) -> str:
        path = self._path(key)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return self.location(key)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put_json(self, key: str, obj: Any) -> str:
        return self.put_bytes(key, json.dumps(obj, indent=2, default=str).encode())

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def location(self, key: str) -> str:
        return f"file://{self._path(key).resolve()}"
