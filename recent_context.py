"""Compact recent-session context shared by Claude hooks and the Pi extension."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

SAME_PROJECT_LIMIT = 7
PROJECT_GROUP_LIMIT = 7
PROJECT_GROUP_CANDIDATE_LIMIT = 70
CROSS_PROJECT_LIMIT = 21
CROSS_PROJECT_DAYS = 7
PROJECT_CONTEXT_CONFIG_PATH = os.path.expanduser("~/.pi/agent/project-context.json")


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


def _project_root_from_cwd(cwd: str) -> str:
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
    return os.path.abspath(project_root)


def _project_from_cwd(cwd: str) -> str:
    return os.path.basename(_project_root_from_cwd(cwd))


def _glob_parts_match(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    memo: dict[tuple[int, int], bool] = {}

    def match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern):
            result = path_index == len(path)
        elif pattern[pattern_index] == "**":
            result = match(pattern_index + 1, path_index) or (
                path_index < len(path) and match(pattern_index, path_index + 1)
            )
        else:
            result = path_index < len(path) and fnmatch.fnmatchcase(
                path[path_index], pattern[pattern_index]
            ) and match(pattern_index + 1, path_index + 1)
        memo[key] = result
        return result

    return match(0, 0)


def _selector_uses_unsupported_glob(selector: str) -> bool:
    """Reject Node-only glob forms that fnmatch would silently misinterpret."""
    return "{" in selector or "}" in selector or any(
        token in selector for token in ("@(", "+(", "?(", "*(", "!(")
    )


def _selector_matches_path(selector: str, path: str) -> bool:
    """Match a configured root selector against path or any ancestor."""
    pattern = tuple(os.path.normpath(os.path.expanduser(selector)).split(os.sep))
    candidate = os.path.abspath(path)
    while True:
        if _glob_parts_match(pattern, tuple(candidate.split(os.sep))):
            return True
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return False
        candidate = parent


def _matching_project_groups(cwd: str) -> list[dict[str, Any]]:
    """Load valid configured groups containing cwd, preserving config order."""
    try:
        with open(PROJECT_CONTEXT_CONFIG_PATH, encoding="utf-8") as config_file:
            raw = json.load(config_file)
    except (OSError, ValueError):
        return []

    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("groups"), list):
        return []

    parsed: list[dict[str, Any]] = []
    names: set[str] = set()
    for candidate in raw["groups"]:
        if not isinstance(candidate, dict):
            return []
        name = candidate.get("name")
        projects = candidate.get("projects")
        files = candidate.get("files")
        if not isinstance(name, str) or not name.strip() or name.strip() in names:
            return []
        if not isinstance(projects, list) or not projects or not all(
            isinstance(selector, str) and selector.strip() for selector in projects
        ):
            return []
        if not isinstance(files, list) or not files or not all(
            isinstance(path, str) and path.strip() for path in files
        ):
            return []

        selectors = [selector.strip() for selector in projects]
        if any(
            selector != "~" and not selector.startswith("~/") and not os.path.isabs(selector)
            for selector in selectors
        ) or any(_selector_uses_unsupported_glob(selector) for selector in selectors):
            return []

        normalized_name = name.strip()
        names.add(normalized_name)
        parsed.append({"name": normalized_name, "selectors": selectors})

    return [
        group for group in parsed
        if any(_selector_matches_path(selector, cwd) for selector in group["selectors"])
    ]


def build_recent_context(cwd: str) -> str | None:
    """Build a compact transcript-routing index for a cwd, or None if empty."""
    if not cwd:
        return None

    from db import (
        DB_PATH,
        get_connection,
        get_headlined_by_project,
        get_headlined_by_project_paths,
        get_headlined_cross_project,
        get_headlined_project_paths,
    )

    if not os.path.exists(DB_PATH):
        return None

    project_root = _project_root_from_cwd(cwd)
    project = os.path.basename(project_root)
    matching_groups = _matching_project_groups(cwd)
    conn = get_connection()
    try:
        same_candidates = get_headlined_by_project(conn, project, project_root)
        since = (datetime.now(timezone.utc) - timedelta(days=CROSS_PROJECT_DAYS)).isoformat()
        cross_candidates = get_headlined_cross_project(conn, since, project)

        group_project_paths: dict[str, set[str]] = {}
        group_candidates: dict[str, list[dict[str, Any]]] = {}
        if matching_groups:
            known_paths = get_headlined_project_paths(conn)
            for group in matching_groups:
                paths = {
                    path for path in known_paths
                    if os.path.abspath(path) != project_root
                    and any(
                        _selector_matches_path(selector, path)
                        for selector in group["selectors"]
                    )
                }
                group_project_paths[group["name"]] = paths
                group_candidates[group["name"]] = get_headlined_by_project_paths(
                    conn,
                    sorted(paths),
                    limit=PROJECT_GROUP_CANDIDATE_LIMIT,
                )
    finally:
        conn.close()

    same_project = [s for s in same_candidates if _session_has_clean_transcript(s)][:SAME_PROJECT_LIMIT]
    group_sections: list[tuple[str, list[dict[str, Any]]]] = []
    for group in matching_groups:
        sessions = [
            session for session in group_candidates[group["name"]]
            if os.path.abspath(session["project_path"]) != project_root
            and _session_has_clean_transcript(session)
        ][:PROJECT_GROUP_LIMIT]
        if sessions:
            group_sections.append((group["name"], sessions))

    excluded_group_paths = {
        path for paths in group_project_paths.values() for path in paths
    }
    cross_existing = [
        session for session in cross_candidates
        if session.get("project_path") not in excluded_group_paths
        and _session_has_clean_transcript(session)
    ]
    cross_project = rank_cross_project_sessions(cross_existing)[:CROSS_PROJECT_LIMIT]

    grouped_sessions = [session for _name, sessions in group_sections for session in sessions]
    if not same_project and not grouped_sessions and not cross_project:
        return None

    example_path = (same_project or grouped_sessions or cross_project)[0]["transcript_path"]
    transcript_root = os.path.dirname(example_path)
    lines = [
        "# Recent Sessions",
        f"Transcript root: {transcript_root}",
        f"Example path: {example_path} (all transcripts, every project, sit flat in this root)",
        "",
        "Read a listed transcript when the user refers to one of these sessions, "
        "or when a headline shows earlier work directly relevant to the task at hand — "
        "sessions often build on one another, and past sessions doing similar work can "
        "carry decisions, lessons, and preferences worth reusing. Transcripts are long, "
        "so read at most the one or two most relevant. "
        "If the needed session is not listed, use the session-search skill.",
    ]

    if same_project:
        lines.append(f"\n## {project} (latest {len(same_project)})")
        lines.extend(f"- {_format_session(session, include_project=False)}" for session in same_project)

    for group_name, sessions in group_sections:
        lines.append(f"\n## {group_name} group (latest {len(sessions)})")
        lines.extend(
            f"- {_format_session(session, include_branch=False)}"
            for session in sessions
        )

    if cross_project:
        lines.append(f"\n## Other projects (top {len(cross_project)} from the last {CROSS_PROJECT_DAYS} days)")
        lines.extend(
            f"- {_format_session(session, include_branch=False)}"
            for session in cross_project
        )

    return "\n".join(lines)
