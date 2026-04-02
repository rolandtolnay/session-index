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


def _format_session(s: dict, *, include_project: bool = True) -> str:
    """Format a session row with full summary."""
    parts = []
    if s.get("started_at"):
        parts.append(s["started_at"][:10])
    if include_project and s.get("project"):
        parts.append(s["project"])
    if s.get("branch"):
        parts.append(f"({s['branch']})")
    if s.get("summary"):
        parts.append(f"— {s['summary']}")
    elif s.get("user_messages"):
        first = s["user_messages"].split("\n---\n")[0][:120]
        parts.append(f"— {first}")
    return " ".join(parts)


def _format_session_short(s: dict) -> str:
    """Format a session row as date + branch only (index entry)."""
    parts = []
    if s.get("started_at"):
        parts.append(s["started_at"][:10])
    if s.get("branch"):
        parts.append(f"({s['branch']})")
    return " ".join(parts)


def _format_cross_project(sessions: list[dict]) -> list[str]:
    """Group cross-project sessions into compact index lines."""
    groups: dict[str, list[dict]] = {}
    for s in sessions:
        proj = s.get("project", "unknown")
        groups.setdefault(proj, []).append(s)

    lines = []
    MAX_BRANCHES = 2
    for project, sess_list in groups.items():
        count = len(sess_list)
        branches = sorted(set(s.get("branch", "") for s in sess_list if s.get("branch")))
        if not branches:
            branch_str = "unknown"
        elif len(branches) <= MAX_BRANCHES:
            branch_str = ", ".join(branches)
        else:
            branch_str = ", ".join(branches[:MAX_BRANCHES]) + f" +{len(branches) - MAX_BRANCHES} more"
        count_str = f"{count} session{'s' if count > 1 else ''}"
        lines.append(f"- {project} — {count_str} ({branch_str})")
    return lines


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

    # Cross-project: try 24h, fall back to 7d if empty
    now = datetime.now(timezone.utc)
    since_24h = (now - timedelta(hours=24)).isoformat()
    cross_project = get_recent_cross_project(conn, since_24h, exclude_project=project, limit=30)
    cross_label = "last 24h"
    if not cross_project:
        since_7d = (now - timedelta(days=7)).isoformat()
        cross_project = get_recent_cross_project(conn, since_7d, exclude_project=project, limit=30)
        cross_label = "last 7d"

    conn.close()

    if not same_project and not cross_project:
        log(session_id, "session_start", "no recent sessions")
        return

    lines = ["# Recent Sessions"]

    # Tiered same-project: first 3 with full summary, rest as index
    FULL_SUMMARY_COUNT = 3
    if same_project:
        lines.append(f"\n## {project} (last {len(same_project)})")
        for i, s in enumerate(same_project):
            if i < FULL_SUMMARY_COUNT:
                lines.append(f"- {_format_session(s, include_project=False)}")
            else:
                lines.append(f"- {_format_session_short(s)}")

    # Cross-project: compact index grouped by project (count + branches)
    if cross_project:
        lines.append(f"\n## Also active ({cross_label})")
        lines.extend(_format_cross_project(cross_project))

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
