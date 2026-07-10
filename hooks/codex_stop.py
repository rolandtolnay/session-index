#!/usr/bin/env python3
"""Codex Stop hook — queue non-blocking automatic session indexing.

Codex exposes Stop at turn scope rather than a distinct SessionEnd event. Each
Stop queues the latest Source Transcript snapshot and starts a detached worker.
The hook itself always emits valid JSON, exits zero, and never waits for
indexing or summarization.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time

# Add the repository root for direct hook execution.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, REPO_ROOT)

from logger import log


DATA_DIR = os.path.expanduser("~/.session-index")
CODEX_JOBS_DIR = os.path.join(DATA_DIR, "codex-jobs")
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_component(value: str, fallback: str = "unknown") -> str:
    cleaned = _SAFE_COMPONENT_RE.sub("-", value).strip("-._")
    return cleaned[:160] or fallback


def session_job_dir(session_id: str) -> str:
    return os.path.join(CODEX_JOBS_DIR, safe_component(session_id))


def _resolve_transcript_path(session_id: str, supplied_path: object) -> str | None:
    if isinstance(supplied_path, str) and supplied_path.strip():
        candidate = os.path.realpath(os.path.expanduser(supplied_path.strip()))
        if os.path.isfile(candidate):
            return candidate

    from sources import discover_codex_sessions

    matches = discover_codex_sessions(session_id)
    if not matches:
        return None
    return max(
        (match.path for match in matches),
        key=lambda path: os.path.getmtime(path) if os.path.exists(path) else 0,
    )


def _queue_job(session_id: str, turn_id: str, transcript_path: str) -> str:
    pending_dir = os.path.join(session_job_dir(session_id), "pending")
    os.makedirs(pending_dir, exist_ok=True)

    stopped_at = time.time()
    suffix = safe_component(turn_id, fallback="turn")
    filename = f"{time.time_ns()}-{os.getpid()}-{suffix}.json"
    payload = {
        "session_id": session_id,
        "turn_id": turn_id,
        "transcript_path": transcript_path,
        "stopped_at": stopped_at,
    }

    fd, temporary_path = tempfile.mkstemp(prefix=".pending-", dir=pending_dir)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        final_path = os.path.join(pending_dir, filename)
        os.replace(temporary_path, final_path)
        return final_path
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def _launch_worker(session_id: str) -> None:
    worker = os.path.join(os.path.dirname(os.path.realpath(__file__)), "_codex_index_worker.py")
    subprocess.Popen(
        [sys.executable, worker, session_id],
        cwd=REPO_ROOT,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _handle_hook() -> None:
    hook_input = json.load(sys.stdin)
    if not isinstance(hook_input, dict):
        return

    session_id = hook_input.get("session_id", "")
    if not isinstance(session_id, str) or not session_id.strip():
        return
    session_id = session_id.strip()

    transcript_path = _resolve_transcript_path(session_id, hook_input.get("transcript_path"))
    if not transcript_path:
        log(session_id, "codex_stop", "source transcript not found")
        return

    turn_id = hook_input.get("turn_id", "")
    turn_id = turn_id.strip() if isinstance(turn_id, str) else ""
    job_path = _queue_job(session_id, turn_id, transcript_path)
    _launch_worker(session_id)
    log(session_id, "codex_stop", f"queued {os.path.basename(job_path)}")


def main() -> None:
    try:
        _handle_hook()
    except Exception as error:
        try:
            log("codex", "codex_stop", f"error: {error}")
        except Exception:
            pass
    finally:
        # Stop hooks require JSON on stdout. Never leak diagnostics here.
        sys.stdout.write("{}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
    raise SystemExit(0)
