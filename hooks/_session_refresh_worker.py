#!/usr/bin/env python3
"""Detached coordinator for provider-neutral active-session refresh jobs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, REPO_ROOT)

from logger import log
from session_refresh import canonical_session_id, session_job_dir


DEFAULT_IDLE_SECONDS = 180.0
DEFAULT_CONTENT_CHARS = 10_000
DEFAULT_CONTENT_COOLDOWN_SECONDS = 60.0
POLL_SECONDS = 1.0

Signature = tuple[tuple[str, int, str], ...]


@dataclass(frozen=True)
class PendingJob:
    path: str
    source: str
    session_id: str
    event_id: str
    transcript_path: str
    observed_at: float
    force_summary: bool


@dataclass
class SummaryAttempt:
    trigger: str
    covered: list[PendingJob]
    signature: Signature
    content_chars: int
    done: threading.Event
    result: object | None = None
    error: Exception | None = None


def _float_setting(name: str, default: float, *, fallback_name: str | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None and fallback_name:
        raw = os.environ.get(fallback_name)
    if raw is None:
        return default
    try:
        value = float(raw)
        return max(0.0, value) if math.isfinite(value) else default
    except ValueError:
        return default


def _idle_seconds(source: str = "claude") -> float:
    fallback = "SESSION_INDEX_CODEX_SUMMARY_IDLE_SECONDS" if source == "codex" else None
    return _float_setting("SESSION_INDEX_SUMMARY_IDLE_SECONDS", DEFAULT_IDLE_SECONDS, fallback_name=fallback)


def _content_chars() -> int:
    return int(_float_setting("SESSION_INDEX_SUMMARY_CONTENT_CHARS", DEFAULT_CONTENT_CHARS))


def _content_cooldown_seconds() -> float:
    return _float_setting(
        "SESSION_INDEX_SUMMARY_CONTENT_COOLDOWN_SECONDS",
        DEFAULT_CONTENT_COOLDOWN_SECONDS,
    )


def _load_pending_jobs(source: str, session_id: str) -> list[PendingJob]:
    pending_dir = os.path.join(session_job_dir(source, session_id), "pending")
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
            transcript_path = payload.get("transcript_path")
            observed_at = float(payload.get("observed_at"))
            if not isinstance(transcript_path, str) or not transcript_path:
                continue
            jobs.append(PendingJob(
                path=path,
                source=source,
                session_id=canonical_session_id(source, str(payload.get("session_id") or session_id)),
                event_id=str(payload.get("event_id") or ""),
                transcript_path=transcript_path,
                observed_at=observed_at,
                force_summary=bool(payload.get("force_summary", False)),
            ))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return sorted(jobs, key=lambda job: (job.observed_at, job.path))


def _remove_jobs(jobs: list[PendingJob], *, keep_latest: bool = False) -> None:
    retained = jobs[-1].path if keep_latest and jobs else None
    for job in jobs:
        if job.path == retained:
            continue
        try:
            os.unlink(job.path)
        except OSError:
            pass


def _normalize_signature(value: object) -> Signature:
    if not isinstance(value, (list, tuple)):
        return ()
    normalized: list[tuple[str, int, str]] = []
    for entry in value:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            return ()
        role, chars, digest = entry
        try:
            normalized.append((str(role), max(0, int(chars)), str(digest)))
        except (TypeError, ValueError):
            return ()
    return tuple(normalized)


def _result_signature(result: object) -> Signature:
    return _normalize_signature(getattr(result, "rendered_content_signature", ()))


def _result_chars(result: object) -> int:
    try:
        return max(0, int(getattr(result, "rendered_content_chars", 0)))
    except (TypeError, ValueError):
        return 0


def _load_state(source: str, session_id: str) -> dict:
    path = os.path.join(session_job_dir(source, session_id), "state.json")
    try:
        with open(path) as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(source: str, session_id: str, state: dict) -> None:
    from session_refresh import _atomic_json

    _atomic_json(os.path.join(session_job_dir(source, session_id), "state.json"), state)


def _signature_json(signature: Signature) -> list[list[object]]:
    return [[role, chars, digest] for role, chars, digest in signature]


def _signature_key(signature: Signature) -> str:
    payload = json.dumps(_signature_json(signature), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _set_baseline(state: dict, signature: Signature, chars: int) -> None:
    state["summary_signature"] = _signature_json(signature)
    state["summary_content_chars"] = chars


def _new_content_chars(state: dict, signature: Signature, current_chars: int) -> int:
    baseline = _normalize_signature(state.get("summary_signature"))
    if not baseline or not signature:
        try:
            return max(0, current_chars - int(state.get("summary_content_chars", 0)))
        except (TypeError, ValueError):
            return current_chars

    common = 0
    for old, new in zip(baseline, signature):
        if old != new:
            break
        common += 1
    if common == len(baseline):
        return sum(entry[1] for entry in signature[common:])
    return sum(entry[1] for entry in signature[common:])


def _has_existing_summary(session_id: str) -> bool:
    from db import get_connection

    conn = get_connection()
    try:
        row = conn.execute("SELECT summary FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return bool(row and row["summary"])
    finally:
        conn.close()


def _run_deterministic(job: PendingJob):
    from indexer import NO_SUMMARY_INDEX_OPTIONS, index_source_transcript

    return index_source_transcript(job.source, job.transcript_path, NO_SUMMARY_INDEX_OPTIONS)


def _run_summary(job: PendingJob):
    from indexer import index_summary

    return index_summary(job.source, job.transcript_path)


def _start_summary_attempt(
    trigger: str,
    covered: list[PendingJob],
    latest: PendingJob,
    signature: Signature,
    content_chars: int,
    summary_index: Callable[[PendingJob], object],
) -> SummaryAttempt:
    attempt = SummaryAttempt(
        trigger=trigger,
        covered=covered,
        signature=signature,
        content_chars=content_chars,
        done=threading.Event(),
    )

    def run() -> None:
        try:
            attempt.result = summary_index(latest)
        except Exception as error:
            attempt.error = error
        finally:
            attempt.done.set()

    threading.Thread(
        target=run,
        name=f"session-summary-{latest.session_id[-12:]}",
        daemon=False,
    ).start()
    return attempt


def _finish_summary_attempt(source: str, session_id: str, attempt: SummaryAttempt) -> None:
    state = _load_state(source, session_id)
    if attempt.error is not None:
        log(session_id, "refresh_worker", f"summary error ({attempt.trigger}): {attempt.error}")
    succeeded = bool(attempt.result and getattr(attempt.result, "summary_generated", False))

    if succeeded:
        log(session_id, "refresh_worker", f"summary generated ({attempt.trigger})")
        if getattr(attempt.result, "headline_generated", False):
            log(session_id, "refresh_worker", f"headline generated ({attempt.trigger})")
        else:
            log(session_id, "refresh_worker", "headline failed (preserved prior value)")
        summary_signature = _result_signature(attempt.result) or attempt.signature
        summary_chars = _result_chars(attempt.result) or attempt.content_chars
        _set_baseline(state, summary_signature, summary_chars)
        state.pop("first_attempt_signature", None)
        state.pop("last_content_attempt_signature", None)
        state["content_attempt_keys"] = []
        _write_state(source, session_id, state)
        _remove_jobs(attempt.covered)
    elif attempt.trigger in {"initial", "content"}:
        log(session_id, "refresh_worker", f"summary failed ({attempt.trigger}; preserved prior value)")
        # Keep one covered snapshot solely for its one idle retry.
        _remove_jobs(attempt.covered, keep_latest=True)
    else:
        log(session_id, "refresh_worker", f"summary failed ({attempt.trigger}; preserved prior value)")
        # Idle and forced attempts consume their covered work even on failure.
        _remove_jobs(attempt.covered)


def _settle_summary_attempt(
    source: str,
    session_id: str,
    attempt: SummaryAttempt,
    sleep: Callable[[float], None],
) -> None:
    while not attempt.done.is_set():
        sleep(POLL_SECONDS)
    _finish_summary_attempt(source, session_id, attempt)


def process_pending_jobs(
    source: str,
    session_id: str,
    *,
    idle_seconds: float | None = None,
    content_chars: int | None = None,
    content_cooldown_seconds: float | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    deterministic_index: Callable[[PendingJob], object] = _run_deterministic,
    summary_index: Callable[[PendingJob], object] = _run_summary,
    has_summary: Callable[[str], bool] = _has_existing_summary,
) -> bool:
    """Process until drained or deterministic failure.

    Summary work runs in a side thread so newer queued turns can refresh
    deterministic artifacts without waiting for the LLM call to finish.
    Returns False only when retained jobs need a future enqueue to retry.
    """
    source = source.strip().lower()
    session_id = canonical_session_id(source, session_id)
    idle_seconds = _idle_seconds(source) if idle_seconds is None else max(0.0, idle_seconds)
    content_chars = _content_chars() if content_chars is None else max(0, int(content_chars))
    cooldown = (
        _content_cooldown_seconds()
        if content_cooldown_seconds is None
        else max(0.0, content_cooldown_seconds)
    )
    deterministic_generation = ""
    deterministic_result: object | None = None
    summary_attempt: SummaryAttempt | None = None

    while True:
        if summary_attempt is not None and summary_attempt.done.is_set():
            _finish_summary_attempt(source, session_id, summary_attempt)
            summary_attempt = None
            # A summary-only pass parsed its snapshot before the LLM call and may
            # have upserted older metadata after a newer deterministic pass.
            deterministic_generation = ""
            deterministic_result = None

        jobs = _load_pending_jobs(source, session_id)
        if not jobs:
            if summary_attempt is not None:
                sleep(POLL_SECONDS)
                continue
            return True
        latest = jobs[-1]

        if latest.path != deterministic_generation:
            if not os.path.isfile(latest.transcript_path):
                log(session_id, "refresh_worker", f"source transcript missing: {latest.transcript_path}")
                if summary_attempt is not None:
                    _settle_summary_attempt(source, session_id, summary_attempt, sleep)
                return False
            try:
                deterministic_result = deterministic_index(latest)
            except Exception as error:
                log(session_id, "refresh_worker", f"deterministic error: {error}")
                if summary_attempt is not None:
                    _settle_summary_attempt(source, session_id, summary_attempt, sleep)
                return False
            skipped_reason = getattr(deterministic_result, "skipped_reason", "")
            if skipped_reason:
                log(session_id, "refresh_worker", f"deterministic skipped ({skipped_reason})")
                # Remove only the snapshot set evaluated as skipped. Jobs that
                # arrive while an older summary settles have not been evaluated.
                skipped_jobs = list(jobs)
                if summary_attempt is not None:
                    _settle_summary_attempt(source, session_id, summary_attempt, sleep)
                    summary_attempt = None
                _remove_jobs(skipped_jobs)
                if _load_pending_jobs(source, session_id):
                    deterministic_generation = ""
                    deterministic_result = None
                    continue
                return True
            deterministic_generation = latest.path
            log(
                session_id,
                "refresh_worker",
                f"deterministic indexed ({getattr(deterministic_result, 'user_message_count', 0)} msgs)",
            )

        assert deterministic_result is not None
        signature = _result_signature(deterministic_result)
        current_chars = _result_chars(deterministic_result)

        # Only one description call runs per session, but deterministic queue
        # draining continues while it is in flight.
        if summary_attempt is not None:
            sleep(POLL_SECONDS)
            continue

        state = _load_state(source, session_id)
        existing_summary = has_summary(getattr(deterministic_result, "session_id", "") or session_id)

        # A pre-coordinator summary owns the current deterministic snapshot as
        # its baseline; do not immediately spend another summary call.
        if "summary_signature" not in state and existing_summary:
            _set_baseline(state, signature, current_chars)
            _write_state(source, session_id, state)

        forced = any(job.force_summary for job in jobs)
        initial = (
            not existing_summary
            and "summary_signature" not in state
            and "first_attempt_signature" not in state
        )
        delta = _new_content_chars(state, signature, current_chars)
        last_content_attempt = float(state.get("last_content_attempt_at", -1e30))
        attempted_keys = {
            str(value) for value in state.get("content_attempt_keys", [])
            if isinstance(value, str)
        }
        legacy_attempt = _normalize_signature(state.get("last_content_attempt_signature"))
        if legacy_attempt:
            attempted_keys.add(_signature_key(legacy_attempt))
        signature_key = _signature_key(signature)
        content_due = (
            existing_summary
            and delta >= content_chars
            and now() - last_content_attempt >= cooldown
            and signature_key not in attempted_keys
        )
        idle_due = now() >= latest.observed_at + idle_seconds

        trigger = ""
        if forced:
            trigger = "force"
        elif initial:
            trigger = "initial"
        elif content_due:
            trigger = "content"
        elif idle_due:
            trigger = "idle"

        if not trigger:
            remaining = max(0.0, latest.observed_at + idle_seconds - now())
            sleep(min(POLL_SECONDS, remaining) if remaining else POLL_SECONDS)
            continue

        covered = list(jobs)
        if trigger == "initial":
            state["first_attempt_signature"] = _signature_json(signature)
        elif trigger == "content":
            state["last_content_attempt_at"] = now()
            attempted_keys.add(signature_key)
            state["content_attempt_keys"] = sorted(attempted_keys)
            state.pop("last_content_attempt_signature", None)
        _write_state(source, session_id, state)
        summary_attempt = _start_summary_attempt(
            trigger,
            covered,
            latest,
            signature,
            current_chars,
            summary_index,
        )


def _pending_exists(source: str, session_id: str) -> bool:
    return bool(_load_pending_jobs(source, session_id))


def _cleanup_pid(source: str, session_id: str) -> bool:
    """Clear our PID under dispatch lock; return False if late work appeared."""
    job_dir = session_job_dir(source, session_id)
    dispatch_path = os.path.join(job_dir, "dispatch.lock")
    pid_path = os.path.join(job_dir, "worker.pid")
    with open(dispatch_path, "a+") as dispatch:
        fcntl.flock(dispatch.fileno(), fcntl.LOCK_EX)
        if _pending_exists(source, session_id):
            return False
        try:
            with open(pid_path) as handle:
                owner = int(handle.read().strip())
        except (OSError, ValueError):
            owner = 0
        if owner in {0, os.getpid()}:
            try:
                os.unlink(pid_path)
            except OSError:
                pass
        return True


def run_worker(source: str, session_id: str) -> None:
    session_id = canonical_session_id(source, session_id)
    job_dir = session_job_dir(source, session_id)
    os.makedirs(job_dir, exist_ok=True)
    with open(os.path.join(job_dir, "worker.lock"), "a+") as worker_lock:
        fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX)
        while True:
            before = {job.path for job in _load_pending_jobs(source, session_id)}
            drained = process_pending_jobs(source, session_id)
            if not drained:
                # Retained deterministic failures are retried only after a new event.
                # Compare under dispatch.lock so an enqueue cannot land between the
                # late-work check and PID cleanup.
                dispatch_path = os.path.join(job_dir, "dispatch.lock")
                with open(dispatch_path, "a+") as dispatch:
                    fcntl.flock(dispatch.fileno(), fcntl.LOCK_EX)
                    after = {job.path for job in _load_pending_jobs(source, session_id)}
                    if after - before:
                        continue
                    pid_path = os.path.join(job_dir, "worker.pid")
                    try:
                        with open(pid_path) as handle:
                            owner = int(handle.read().strip())
                        if owner == os.getpid():
                            os.unlink(pid_path)
                    except (OSError, ValueError):
                        pass
                return
            if _cleanup_pid(source, session_id):
                return


def main() -> None:
    if len(sys.argv) >= 3:
        run_worker(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        try:
            sid = sys.argv[2] if len(sys.argv) >= 3 else "refresh"
            log(sid, "refresh_worker", f"error: {error}")
        except Exception:
            pass
    raise SystemExit(0)
