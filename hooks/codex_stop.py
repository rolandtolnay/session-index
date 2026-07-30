#!/usr/bin/env python3
"""Codex Stop hook — queue non-blocking active-session refresh.

Codex exposes Stop at turn scope rather than a distinct SessionEnd event. Each
Stop queues the latest Source Transcript snapshot for the shared coordinator.
The hook always emits valid JSON, exits zero, and never waits for indexing or
summarization.
"""

from __future__ import annotations

import json
import os
import sys

# Add the repository root for direct hook execution.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, REPO_ROOT)

from logger import log
from session_refresh import enqueue_refresh


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
    job_path = enqueue_refresh(
        "codex",
        session_id,
        transcript_path,
        event_id=turn_id,
    )
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
