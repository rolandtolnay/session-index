"""Coordinate indexing with an offline identity migration (POSIX local store)."""

from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path


def require_indexing_enabled(data_dir: str | Path) -> None:
    if (Path(data_dir) / "identity-migration.json").exists():
        raise RuntimeError("Session Index identity migration is active; indexing is paused")


@contextmanager
def indexing_lock(data_dir: str | Path, session_id: str | None = None, *, blocking: bool = True):
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    require_indexing_enabled(root)
    with (root / "indexing.lock").open("a+") as global_lock:
        fcntl.flock(global_lock, fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB))
        require_indexing_enabled(root)
        if session_id is None:
            yield
            return
        locks = root / "session-locks"
        locks.mkdir(exist_ok=True)
        name = hashlib.sha256(session_id.encode()).hexdigest()
        with (locks / name).open("a+") as session_lock:
            fcntl.flock(session_lock, fcntl.LOCK_EX)
            yield
