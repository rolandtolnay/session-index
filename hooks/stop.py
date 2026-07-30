#!/usr/bin/env python3
"""Claude Stop hook — queue non-blocking active-session refresh.

The shared detached coordinator refreshes deterministic artifacts immediately,
creates the first summary/headline for a qualifying session, and schedules later
summary refreshes. Loop prevention uses stop_hook_active from stdin.
"""

import json
import os
import sys

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log
from session_refresh import enqueue_refresh


def main() -> None:
    # Guard against recursive execution from claude -p subprocesses
    if os.environ.get("_CLAUDE_HOOK_NESTED"):
        return

    hook_input = json.load(sys.stdin)

    # Claude Code sets this when a prior Stop hook triggered a continuation.
    if hook_input.get("stop_hook_active"):
        return

    session_id = hook_input.get("session_id", "")
    jsonl_path = hook_input.get("transcript_path", "")
    if not session_id or not jsonl_path:
        return

    if not os.path.exists(jsonl_path):
        log(session_id, "stop", f"jsonl not found: {jsonl_path}")
        return

    job_path = enqueue_refresh("claude", session_id, jsonl_path, event_id="stop")
    log(session_id, "stop", f"queued {os.path.basename(job_path)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log("??????", "stop", f"error: {e}")
        except Exception:
            pass
    sys.exit(0)
