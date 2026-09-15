"""
Atomic JSON persistence.

The inventory, the subnet list and the user list are all small JSON files that
get rewritten whole. Writing them in place would let a crash -- or an IIS
app-pool recycle landing mid-write -- truncate the file, so every write goes to
a temporary file in the same directory and is renamed over the target.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def write_atomic(path: Path, payload: Any) -> None:
    """Serialise `payload` and replace `path` with it in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    temp_name = handle.name
    try:
        with handle:
            json.dump(payload, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except OSError:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Any:
    """Read a JSON file, returning None if it is missing or unparseable."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def quarantine(path: Path) -> None:
    """Move a corrupt file aside so startup continues and it can be inspected."""
    try:
        path.replace(path.with_suffix(path.suffix + f".corrupt-{int(time.time())}"))
    except OSError:
        pass
