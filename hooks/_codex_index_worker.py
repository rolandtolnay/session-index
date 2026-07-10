#!/usr/bin/env python3
"""Detached coordinator for automatic Codex Stop indexing.

Pending jobs are serialized per session. Deterministic artifacts are refreshed
as soon as a new Stop arrives; the summary is refreshed once the session has
been idle for the configured interval.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable

# Add the repository root for detached direct execution.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, REPO_ROOT)

from logger import log

from codex_stop import session_job_dir


DEFAULT_IDLE_SECONDS = 300.0
POLL_SECONDS = 1.0


@dataclass(frozen=True)
class PendingJob:
    path: str
    session_id: str
    turn_id: str
    transcript_path: str
    stopped_at: float


def _idle_seconds() -> float:
    raw = os.environ.get("SESSION_INDEX_CODEX_SUMMARY_IDLE_SECONDS", str(DEFAULT_IDLE_SECONDS))
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_IDLE_SECONDS


def _load_pending_jobs(session_id: str) -> list[PendingJob]:
    pending_dir = os.path.join(session_job_dir(session_id), "pending")
    try:
        names = os.listdir(pending_dir)
    except OSError:
        return []

    jobs: list[PendingJob] = []
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        path = os.path.join(pending_dir, name)
        try:
            with open(path) as handle:
                payload = json.load(handle)
            transcript_path = payload.get("transcript_path", "")
            stopped_at = float(payload.get("stopped_at", 0))
            if not isinstance(transcript_path, str) or not transcript_path or stopped_at <= 0:
                continue
            jobs.append(PendingJob(
                path=path,
                session_id=str(payload.get("session_id") or session_id),
                turn_id=str(payload.get("turn_id") or ""),
                transcript_path=transcript_path,
                stopped_at=stopped_at,
            ))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return sorted(jobs, key=lambda job: (job.stopped_at, job.path))


def _remove_jobs(jobs: list[PendingJob]) -> None:
    for job in jobs:
        try:
            os.unlink(job.path)
        except OSError:
            pass


def _run_deterministic(job: PendingJob):
    from indexer import NO_SUMMARY_INDEX_OPTIONS, index_source_transcript

    return index_source_transcript("codex", job.transcript_path, NO_SUMMARY_INDEX_OPTIONS)


def _run_summary(job: PendingJob):
    from indexer import index_summary

    return index_summary("codex", job.transcript_path)


def process_pending_jobs(
    session_id: str,
    *,
    idle_seconds: float | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    deterministic_index: Callable[[PendingJob], object] = _run_deterministic,
    summary_index: Callable[[PendingJob], object] = _run_summary,
) -> None:
    idle_seconds = _idle_seconds() if idle_seconds is None else max(0.0, idle_seconds)
    deterministic_generation = ""

    while True:
        jobs = _load_pending_jobs(session_id)
        if not jobs:
            return
        latest = jobs[-1]

        if latest.path != deterministic_generation:
            if not os.path.isfile(latest.transcript_path):
                log(session_id, "codex_worker", f"source transcript missing: {latest.transcript_path}")
                return
            try:
                result = deterministic_index(latest)
            except Exception as error:
                log(session_id, "codex_worker", f"deterministic error: {error}")
                return
            skipped_reason = getattr(result, "skipped_reason", "")
            if skipped_reason:
                log(session_id, "codex_worker", f"deterministic skipped ({skipped_reason})")
                _remove_jobs(jobs)
                return
            deterministic_generation = latest.path
            log(
                session_id,
                "codex_worker",
                f"deterministic indexed ({getattr(result, 'user_message_count', 0)} msgs)",
            )

        remaining = latest.stopped_at + idle_seconds - now()
        if remaining > 0:
            sleep(min(POLL_SECONDS, remaining))
            continue

        # Snapshot the jobs covered by this summary attempt. A Stop arriving
        # during summarization creates another job, which remains for the next
        # loop/queued worker.
        covered = list(jobs)
        try:
            result = summary_index(latest)
            if getattr(result, "summary_generated", False):
                log(session_id, "codex_worker", "summary generated")
            else:
                log(session_id, "codex_worker", "summary failed (preserved prior value)")
        except Exception as error:
            log(session_id, "codex_worker", f"summary error: {error}")
        finally:
            _remove_jobs(covered)

        # Loop so a Stop that arrived during the summary is indexed immediately.
        deterministic_generation = ""


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        return
    session_id = sys.argv[1].strip()
    job_dir = session_job_dir(session_id)
    os.makedirs(job_dir, exist_ok=True)
    lock_path = os.path.join(job_dir, "worker.lock")

    # Each Stop launches a worker. Blocking here closes the handoff race: a
    # worker queued behind the active coordinator will either process a late
    # job or observe that the active worker already covered everything.
    with open(lock_path, "a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        process_pending_jobs(session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        try:
            sid = sys.argv[1] if len(sys.argv) > 1 else "codex"
            log(sid, "codex_worker", f"error: {error}")
        except Exception:
            pass
    raise SystemExit(0)
