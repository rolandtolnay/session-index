#!/usr/bin/env python3
"""SessionEnd hook — launches detached worker for LLM summary + transcript.

Critical constraint: SessionEnd timeout is ~1.5s, LLM takes ~2.4s.
Solution: fork detached subprocess via Popen(start_new_session=True), exit immediately.
"""

import json
import os
import subprocess
import sys

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log


def main() -> None:
    # Guard against recursive execution from claude -p subprocesses
    if os.environ.get("_CLAUDE_HOOK_NESTED"):
        return

    hook_input = json.load(sys.stdin)
    session_id = hook_input.get("session_id", "")
    transcript_path = hook_input.get("transcript_path", "")

    if not session_id or not transcript_path:
        return

    log(session_id, "session_end", "launching worker")

    worker = os.path.join(os.path.dirname(os.path.realpath(__file__)), "_session_end_worker.py")

    # Fork detached subprocess — parent exits immediately
    subprocess.Popen(
        [sys.executable, worker, session_id, transcript_path],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    log(session_id, "session_end", "worker launched")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log("??????", "session_end", f"error: {e}")
        except Exception:
            pass
    sys.exit(0)
