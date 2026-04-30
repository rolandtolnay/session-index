#!/usr/bin/env python3
"""Stop hook — upsert deterministic fields on every Stop event.

Parses the JSONL, checks >= 1 user + 1 assistant message, upserts deterministic
fields only (no summary, no transcript). Loop-prevention via
stop_hook_active field from stdin.
"""

import json
import os
import sys

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log


def main() -> None:
    # Guard against recursive execution from claude -p subprocesses
    if os.environ.get("_CLAUDE_HOOK_NESTED"):
        return

    hook_input = json.load(sys.stdin)

    # Loop prevention: Claude Code sets this when a prior Stop hook triggered a continuation
    if hook_input.get("stop_hook_active"):
        return

    session_id = hook_input.get("session_id", "")
    jsonl_path = hook_input.get("transcript_path", "")

    if not session_id or not jsonl_path:
        return

    log(session_id, "stop", "started")

    if not os.path.exists(jsonl_path):
        log(session_id, "stop", f"jsonl not found: {jsonl_path}")
        return

    from indexer import index_fast

    result = index_fast("claude", jsonl_path)
    if result.skipped_reason:
        log(session_id, "stop", f"skipped ({result.skipped_reason})")
        return

    log(session_id, "stop", f"upserted ({result.user_message_count} msgs, {result.files_touched} files)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log("??????", "stop", f"error: {e}")
        except Exception:
            pass
    sys.exit(0)
