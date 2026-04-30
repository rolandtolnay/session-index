"""Recent-session context builder shared by Claude hooks and Pi extension."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone


def _format_session(s: dict, *, include_project: bool = True) -> str:
    """Format a session row with full summary."""
    parts = []
    if s.get("started_at"):
        parts.append(s["started_at"][:10])
    if include_project and s.get("project"):
        parts.append(s["project"])
    if s.get("branch"):
        parts.append(f"({s['branch']})")
    if s.get("source") and s.get("source") != "claude":
        parts.append(f"[{s['source']}]")
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
    if s.get("source") and s.get("source") != "claude":
        parts.append(f"[{s['source']}]")
    return " ".join(parts)


def _format_cross_project(sessions: list[dict]) -> list[str]:
    """Group cross-project sessions into compact index lines."""
    groups: dict[str, list[dict]] = {}
    for s in sessions:
        proj = s.get("project", "unknown")
        groups.setdefault(proj, []).append(s)

    lines = []
    max_branches = 2
    for project, sess_list in groups.items():
        count = len(sess_list)
        branches = sorted(set(s.get("branch", "") for s in sess_list if s.get("branch")))
        sources = sorted(set(s.get("source", "") for s in sess_list if s.get("source") and s.get("source") != "claude"))
        if not branches:
            branch_str = "unknown"
        elif len(branches) <= max_branches:
            branch_str = ", ".join(branches)
        else:
            branch_str = ", ".join(branches[:max_branches]) + f" +{len(branches) - max_branches} more"
        source_str = f" [{', '.join(sources)}]" if sources else ""
        count_str = f"{count} session{'s' if count > 1 else ''}"
        lines.append(f"- {project}{source_str} — {count_str} ({branch_str})")
    return lines


def _project_from_cwd(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        project_root = result.stdout.strip() if result.returncode == 0 else cwd
    except Exception:
        project_root = cwd
    return os.path.basename(project_root)


def build_recent_context(cwd: str) -> str | None:
    """Build markdown recent-session context for a cwd, or None if empty."""
    if not cwd:
        return None

    from db import DB_PATH, get_connection, get_recent_by_project, get_recent_cross_project

    if not os.path.exists(DB_PATH):
        return None

    project = _project_from_cwd(cwd)
    conn = get_connection()

    same_project = get_recent_by_project(conn, project, limit=5)

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
        return None

    lines = ["# Recent Sessions"]

    full_summary_count = 3
    if same_project:
        lines.append(f"\n## {project} (last {len(same_project)})")
        for i, s in enumerate(same_project):
            if i < full_summary_count:
                lines.append(f"- {_format_session(s, include_project=False)}")
            else:
                lines.append(f"- {_format_session_short(s)}")

    if cross_project:
        lines.append(f"\n## Also active ({cross_label})")
        lines.extend(_format_cross_project(cross_project))

    return "\n".join(lines)
