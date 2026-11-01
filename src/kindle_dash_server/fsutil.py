"""Small filesystem helpers."""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def atomic_write(path: Path, data: bytes | str) -> None:
    """Write via a uniquely named temp file + rename: readers never see a partial file, and
    concurrent writers of the same path (a scheduled render and a preview) don't collide."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        if isinstance(data, str):
            tmp.write_text(data, encoding="utf-8")
        else:
            tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
