#!/usr/bin/env python3
"""SessionStart hook — inject recent session context.

Queries: last 5 same-project (any age) + last 24h cross-project.
Outputs JSON with additionalContext to stdout.
No LLM calls — must be fast.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log


def _format_session(s: dict) -> str:
    """Format a session row into a compact context line."""
    parts = []
    if s.get("started_at"):
        # Just the date
        parts.append(s["started_at"][:10])
    if s.get("project"):
        parts.append(s["project"])
    if s.get("branch"):
        parts.append(f"({s['branch']})")
    if s.get("summary"):
        parts.append(f"— {s['summary']}")
    elif s.get("user_messages"):
        # First user message as fallback, truncated
        first = s["user_messages"].split("\n---\n")[0][:120]
        parts.append(f"— {first}")
    return " ".join(parts)


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

    # Derive project name from git root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        project_root = result.stdout.strip() if result.returncode == 0 else cwd
    except Exception:
        project_root = cwd

    project = os.path.basename(project_root)

    from db import get_connection, get_recent_by_project, get_recent_cross_project, DB_PATH

    if not os.path.exists(DB_PATH):
        log(session_id, "session_start", "no db yet")
        return

    conn = get_connection()

    # Last 5 same-project sessions
    same_project = get_recent_by_project(conn, project, limit=5)

    # Last 24h cross-project
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cross_project = get_recent_cross_project(conn, since, exclude_project=project, limit=10)

    conn.close()

    if not same_project and not cross_project:
        log(session_id, "session_start", "no recent sessions")
        return

    lines = ["# Recent Sessions"]

    if same_project:
        lines.append(f"\n## {project} (last {len(same_project)})")
        for s in same_project:
            lines.append(f"- {_format_session(s)}")

    if cross_project:
        lines.append("\n## Other projects (last 24h)")
        for s in cross_project:
            lines.append(f"- {_format_session(s)}")

    context = "\n".join(lines)

    # Output as JSON with additionalContext
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))

    log(session_id, "session_start", f"injected {len(same_project)} same + {len(cross_project)} cross")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log("??????", "session_start", f"error: {e}")
        except Exception:
            pass
    sys.exit(0)
