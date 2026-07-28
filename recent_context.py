"""Compact recent-session context shared by Claude hooks and the Pi extension."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

SAME_PROJECT_LIMIT = 7
CROSS_PROJECT_LIMIT = 21
CROSS_PROJECT_DAYS = 7


def _format_session(
    s: dict[str, Any],
    *,
    include_project: bool = True,
    include_branch: bool = True,
) -> str:
    """Format one routing headline with selected metadata and a transcript name."""
    parts: list[str] = []
    if s.get("started_at"):
        parts.append(s["started_at"][:10])
    if include_project and s.get("project"):
        parts.append(s["project"])
    if include_branch and s.get("branch"):
        parts.append(f"({s['branch']})")
    if s.get("transcript_path"):
        parts.append(f"`{os.path.basename(s['transcript_path'])}`")

    headline = (s.get("headline") or "").strip()
    if headline:
        parts.append(f"— {headline}")
    return " ".join(parts)


def _session_has_clean_transcript(session: dict[str, Any]) -> bool:
    path = session.get("transcript_path")
    return bool(path and os.path.isfile(path))


def _ranking_facts(session: dict[str, Any]) -> tuple[int, int]:
    """Return total turns and assistant characters, deriving exact legacy metrics."""
    user_turns = max(0, int(session.get("user_message_count") or 0))
    assistant_count = session.get("assistant_message_count")
    assistant_chars = session.get("assistant_char_count")
    if assistant_count is None or assistant_chars is None:
        try:
            from transcript import read_assistant_metrics

            assistant_count, assistant_chars = read_assistant_metrics(session["transcript_path"])
        except (OSError, TypeError, KeyError):
            assistant_count, assistant_chars = 0, 0
    return user_turns + max(0, int(assistant_count)), max(0, int(assistant_chars))


def _percentile_rank(value: int, values: list[int]) -> float:
    """Return an average-tie percentile rank in [0, 1]."""
    if len(values) <= 1:
        return 1.0
    below = sum(candidate < value for candidate in values)
    equal = sum(candidate == value for candidate in values)
    average_index = below + (equal - 1) / 2
    return average_index / (len(values) - 1)


def rank_cross_project_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank sessions by interaction depth and substantive assistant output."""
    if not sessions:
        return []

    facts = [_ranking_facts(session) for session in sessions]
    turn_values = [turns for turns, _chars in facts]
    char_values = [chars for _turns, chars in facts]
    scored = []
    for session, (turns, chars) in zip(sessions, facts):
        score = 0.6 * _percentile_rank(turns, turn_values) + 0.4 * _percentile_rank(chars, char_values)
        scored.append((score, session.get("started_at") or "", session.get("session_id") or "", session))
    scored.sort(key=lambda item: item[:3], reverse=True)
    return [session for _score, _started_at, _session_id, session in scored]


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
    """Build a compact transcript-routing index for a cwd, or None if empty."""
    if not cwd:
        return None

    from db import DB_PATH, get_connection, get_headlined_by_project, get_headlined_cross_project

    if not os.path.exists(DB_PATH):
        return None

    project = _project_from_cwd(cwd)
    conn = get_connection()
    try:
        same_candidates = get_headlined_by_project(conn, project)
        since = (datetime.now(timezone.utc) - timedelta(days=CROSS_PROJECT_DAYS)).isoformat()
        cross_candidates = get_headlined_cross_project(conn, since, project)
    finally:
        conn.close()

    same_project = [s for s in same_candidates if _session_has_clean_transcript(s)][:SAME_PROJECT_LIMIT]
    cross_existing = [s for s in cross_candidates if _session_has_clean_transcript(s)]
    cross_project = rank_cross_project_sessions(cross_existing)[:CROSS_PROJECT_LIMIT]

    if not same_project and not cross_project:
        return None

    transcript_root = os.path.dirname((same_project or cross_project)[0]["transcript_path"])
    lines = [
        "# Recent Sessions",
        f"Transcript root: {transcript_root}",
        "",
        "Read a listed transcript when the user refers to one of these sessions. "
        "If the needed session is not listed, use the session-search skill.",
    ]

    if same_project:
        lines.append(f"\n## {project} (latest {len(same_project)})")
        lines.extend(f"- {_format_session(session, include_project=False)}" for session in same_project)

    if cross_project:
        lines.append(f"\n## Other projects (top {len(cross_project)} from the last {CROSS_PROJECT_DAYS} days)")
        lines.extend(
            f"- {_format_session(session, include_branch=False)}"
            for session in cross_project
        )

    return "\n".join(lines)
