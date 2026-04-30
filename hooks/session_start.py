#!/usr/bin/env python3
"""SessionStart hook — inject recent session context.

Queries: last 5 same-project (any age) + last 24h cross-project.
Outputs Claude hook JSON with additionalContext to stdout.
No LLM calls — must be fast.
"""

import json
import os
import sys

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log
from recent_context import (
    build_recent_context,
    _format_cross_project,
    _format_session,
    _format_session_short,
)


def main() -> None:
    # Guard against recursive execution from claude -p subprocesses
    if os.environ.get("_CLAUDE_HOOK_NESTED"):
        return

    hook_input = json.load(sys.stdin)
    session_id = hook_input.get("session_id", "")
    cwd = hook_input.get("cwd", "")

    if not cwd:
        return

    log(session_id, "session_start", "started")
    context = build_recent_context(cwd)

    if not context:
        log(session_id, "session_start", "no recent sessions")
        return

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))
    log(session_id, "session_start", "injected recent context")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log("??????", "session_start", f"error: {e}")
        except Exception:
            pass
    sys.exit(0)
