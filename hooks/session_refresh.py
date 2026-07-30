#!/usr/bin/env python3
"""Provider-neutral queue for non-blocking active-session refreshes."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DATA_DIR = os.path.expanduser("~/.session-index")
REFRESH_JOBS_DIR = os.path.join(DATA_DIR, "refresh-jobs")
_SUPPORTED_SOURCES = {"claude", "pi", "codex"}
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _source_name(source: str) -> str:
    normalized = (source or "").strip().lower()
    if normalized not in _SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported session source: {source}")
    return normalized


def canonical_session_id(source: str, session_id: str) -> str:
    """Return the database-form session id without duplicating a provider prefix."""
    source = _source_name(source)
    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("session_id is required")
    if source == "claude":
        return session_id.removeprefix("claude:")
    prefix = f"{source}:"
    return session_id if session_id.startswith(prefix) else f"{prefix}{session_id}"


def _safe_component(value: str, fallback: str = "unknown") -> str:
    cleaned = _SAFE_COMPONENT_RE.sub("-", value).strip("-._")
    return cleaned[:200] or fallback


def session_job_dir(source: str, session_id: str) -> str:
    source = _source_name(source)
    canonical_id = canonical_session_id(source, session_id)
    return os.path.join(REFRESH_JOBS_DIR, source, _safe_component(canonical_id))


def _atomic_json(path: str, payload: object) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_pid(path: str) -> int:
    try:
        with open(path) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return 0


def _launch_worker(source: str, session_id: str) -> int:
    worker = os.path.join(os.path.dirname(os.path.realpath(__file__)), "_session_refresh_worker.py")
    process = subprocess.Popen(
        [sys.executable, worker, source, session_id],
        cwd=REPO_ROOT,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process.pid


def _ensure_worker(source: str, session_id: str) -> int:
    job_dir = session_job_dir(source, session_id)
    os.makedirs(job_dir, exist_ok=True)
    dispatch_path = os.path.join(job_dir, "dispatch.lock")
    pid_path = os.path.join(job_dir, "worker.pid")
    with open(dispatch_path, "a+") as dispatch:
        fcntl.flock(dispatch.fileno(), fcntl.LOCK_EX)
        pid = _read_pid(pid_path)
        if _pid_is_alive(pid):
            return pid
        try:
            os.unlink(pid_path)
        except OSError:
            pass
        pid = _launch_worker(source, canonical_session_id(source, session_id))
        _atomic_json(pid_path, pid)
        return pid


def enqueue_refresh(
    source: str,
    session_id: str,
    transcript_path: str,
    event_id: str = "",
    observed_at: float | None = None,
    force_summary: bool = False,
) -> str:
    """Atomically enqueue a snapshot and ensure one detached coordinator exists.

    This is a library API and intentionally raises. Hook/CLI callers own their
    non-throwing boundary.
    """
    source = _source_name(source)
    session_id = canonical_session_id(source, session_id)
    transcript_path = (transcript_path or "").strip()
    if not transcript_path:
        raise ValueError("transcript_path is required")
    transcript_path = os.path.realpath(os.path.expanduser(transcript_path))
    observed_at = time.time() if observed_at is None else float(observed_at)

    pending_dir = os.path.join(session_job_dir(source, session_id), "pending")
    os.makedirs(pending_dir, exist_ok=True)
    suffix = _safe_component(event_id, "event")
    filename = f"{time.time_ns()}-{os.getpid()}-{suffix}.json"
    final_path = os.path.join(pending_dir, filename)
    _atomic_json(final_path, {
        "event_id": str(event_id or ""),
        "force_summary": bool(force_summary),
        "observed_at": observed_at,
        "session_id": session_id,
        "source": source,
        "transcript_path": transcript_path,
    })
    _ensure_worker(source, session_id)
    return final_path


def main() -> None:
    """Minimal non-throwing JSON CLI for provider adapters."""
    try:
        payload = json.load(sys.stdin)
        enqueue_refresh(
            payload["source"],
            payload["session_id"],
            payload["transcript_path"],
            event_id=payload.get("event_id", ""),
            observed_at=payload.get("observed_at"),
            force_summary=payload.get("force_summary", False),
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
    raise SystemExit(0)
