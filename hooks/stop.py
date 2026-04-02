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

    from parser import parse_jsonl
    from db import init_db, get_connection, upsert_session

    session = parse_jsonl(jsonl_path)

    if session.user_message_count < 1 or session.assistant_message_count < 1:
        log(session_id, "stop", f"skipped ({session.user_message_count} user, {session.assistant_message_count} assistant msgs)")
        return

    conn = get_connection()
    init_db(conn)

    upsert_session(
        conn,
        session_id=session.session_id,
        slug=session.slug or None,
        project_path=session.project_path or None,
        project=session.project or None,
        branch=session.branch or None,
        model=session.model or None,
        started_at=session.started_at or None,
        ended_at=session.ended_at or None,
        duration_seconds=session.duration_seconds or None,
        user_message_count=session.user_message_count,
        user_messages="\n---\n".join(session.user_messages) if session.user_messages else None,
        files_touched=", ".join(session.files_touched) if session.files_touched else None,
        tools_used=session.tools_used or None,
        # No summary or transcript — those come from the worker
    )
    conn.close()

    log(session_id, "stop", f"upserted ({session.user_message_count} msgs, {len(session.files_touched)} files)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log("??????", "stop", f"error: {e}")
        except Exception:
            pass
    sys.exit(0)
