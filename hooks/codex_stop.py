#!/usr/bin/env python3
"""Codex lifecycle hooks — queue non-blocking active-session refresh.

Stop/Interrupt queue turns; SessionEnd forces a final refresh. SessionStart
injects recent-session context and recovers stranded work in a detached process.
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
    else:
        candidate = ""
    from sources import resolve_codex_source
    return resolve_codex_source(session_id, candidate)


def _handle_hook() -> dict | None:
    hook_input = json.load(sys.stdin)
    if not isinstance(hook_input, dict):
        return

    event = hook_input.get("hook_event_name", "Stop")
    if event == "SessionStart":
        from session_refresh import launch_recovery
        from recent_context import build_recent_context
        try:
            launch_recovery("codex")
        except Exception as error:
            log("codex", "codex_start", f"recovery launch failed: {error}")
        cwd = hook_input.get("cwd")
        context = build_recent_context(cwd) if isinstance(cwd, str) and cwd.strip() else None
        if context:
            return {"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": context,
            }}
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
    options = {"force_summary": True} if event == "SessionEnd" else {}
    job_path = enqueue_refresh(
        "codex",
        session_id,
        transcript_path,
        event_id=turn_id,
        **options,
    )
    log(session_id, "codex_stop", f"queued {os.path.basename(job_path)}")


def main() -> None:
    output = {}
    try:
        output = _handle_hook() or {}
    except Exception as error:
        try:
            log("codex", "codex_stop", f"error: {error}")
        except Exception:
            pass
    finally:
        # Stop hooks require JSON on stdout. Never leak diagnostics here.
        sys.stdout.write(json.dumps(output) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
    raise SystemExit(0)
