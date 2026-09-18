"""Serialize artifact writes and deletion for each session (POSIX local store)."""

from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def indexing_lock(data_dir: str | Path, session_id: str):
    locks = Path(data_dir) / "session-locks"
    locks.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(session_id.encode()).hexdigest()
    with (locks / name).open("a+") as session_lock:
        fcntl.flock(session_lock, fcntl.LOCK_EX)
        yield
