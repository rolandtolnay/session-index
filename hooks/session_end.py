#!/usr/bin/env python3
"""Claude SessionEnd hook — queue a forced final session refresh.

The hook only persists a job and ensures the detached shared coordinator is
running, so Claude's short SessionEnd timeout never waits for indexing or LLMs.
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
    session_id = hook_input.get("session_id", "")
    transcript_path = hook_input.get("transcript_path", "")
    if not session_id or not transcript_path:
        return

    if not os.path.exists(transcript_path):
        log(session_id, "session_end", f"jsonl not found: {transcript_path}")
        return

    job_path = enqueue_refresh(
        "claude",
        session_id,
        transcript_path,
        event_id="session-end",
        force_summary=True,
    )
    log(session_id, "session_end", f"queued final refresh {os.path.basename(job_path)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log("??????", "session_end", f"error: {e}")
        except Exception:
            pass
    sys.exit(0)
