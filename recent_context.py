"""Compact recent-session context shared by Claude/Codex hooks and the Pi extension."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from project_groups import PROJECT_CONTEXT_CONFIG_PATH, load_project_groups, selector_matches_path
from project_identity import resolve_project

SAME_PROJECT_LIMIT = 7
PROJECT_GROUP_LIMIT = 14
CROSS_PROJECT_LIMIT = 21
ACTIVE_DAYS = 7


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


def _recency_key(session: dict[str, Any]) -> tuple[datetime, str]:
    try:
        started = datetime.fromisoformat(session.get("started_at") or "")
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except ValueError:
        started = datetime.min.replace(tzinfo=timezone.utc)
    return started, session.get("session_id") or ""


def select_active_day_sessions(
    sessions: list[dict[str, Any]], *, limit: int, ensure_project_coverage: bool = False,
) -> list[dict[str, Any]]:
    """Rank a pool's seven most recent active UTC dates, independent of elapsed time.

    Candidates are eligible, non-future sessions with existing clean transcripts.
    Only substantial, useful, or unclassified sessions establish active dates.
    Low-value sessions on those dates can represent otherwise absent group projects;
    coverage may exceed the soft limit. Unknown bands compete with useful ones.
    """
    if limit <= 0:
        return []
    ranked = sorted(sessions, key=_recency_key, reverse=True)
    active_dates = set(sorted({
        _recency_key(session)[0].astimezone(timezone.utc).date()
        for session in ranked if session.get("substance_band") != "low_value"
    }, reverse=True)[:ACTIVE_DAYS])
    ranked = [
        session for session in ranked
        if _recency_key(session)[0].astimezone(timezone.utc).date() in active_dates
    ]
    # Stable sort preserves actual instant ordering and deterministic ID ties.
    priorities = {"substantial": 0, "useful": 1, "low_value": 2}
    ranked.sort(key=lambda s: priorities.get(s.get("substance_band"), 1))
    selected: set[str] = set()
    if ensure_project_coverage:
        projects: set[str] = set()
        for session in ranked:
            project = session.get("project_id") or session.get("project_path") or session.get("project") or ""
            if project not in projects:
                projects.add(project)
                selected.add(session["session_id"])
    for session in ranked:
        if len(selected) >= limit:
            break
        if session.get("substance_band") != "low_value":
            selected.add(session["session_id"])
    return [session for session in ranked if session["session_id"] in selected]


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

    identity = resolve_project(cwd)
    project_root = identity.project_path
    project = identity.project
    matching_groups = [
        group for group in load_project_groups(PROJECT_CONTEXT_CONFIG_PATH)
        if any(selector_matches_path(selector, project_root) for selector in group["selectors"])
    ]
    conn = get_connection()
    try:
        same_candidates = get_headlined_by_project(conn, project, project_root, project_id=identity.project_id)
        until = datetime.now(timezone.utc).isoformat()
        cross_candidates = get_headlined_cross_project(
            conn, project, until=until, project_path=project_root, project_id=identity.project_id,
        )

        group_project_paths: dict[str, set[str]] = {}
        group_candidates: dict[str, list[dict[str, Any]]] = {}
        if matching_groups:
            known_paths = get_headlined_project_paths(conn)
            for group in matching_groups:
                paths = {
                    path for path in known_paths
                    if os.path.abspath(path) != project_root
                    and any(
                        selector_matches_path(selector, path)
                        for selector in group["selectors"]
                    )
                }
                group_project_paths[group["name"]] = paths
                group_candidates[group["name"]] = get_headlined_by_project_paths(
                    conn,
                    sorted(paths),
                    until=until,
                )
    finally:
        conn.close()

    same_project = [s for s in same_candidates if _session_has_clean_transcript(s)][:SAME_PROJECT_LIMIT]
    group_sections: list[tuple[str, list[dict[str, Any]]]] = []
    for group in matching_groups:
        sessions = select_active_day_sessions([
            session for session in group_candidates[group["name"]]
            if os.path.abspath(session["project_path"]) != project_root
            and _session_has_clean_transcript(session)
        ], limit=PROJECT_GROUP_LIMIT, ensure_project_coverage=True)
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
    cross_project = select_active_day_sessions(cross_existing, limit=CROSS_PROJECT_LIMIT)

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
        lines.append(f"\n## {group_name} group (top {len(sessions)} from the last {ACTIVE_DAYS} active days)")
        lines.extend(
            f"- {_format_session(session, include_branch=False)}"
            for session in sessions
        )

    if cross_project:
        lines.append(f"\n## Other projects (top {len(cross_project)} from the last {ACTIVE_DAYS} active days)")
        lines.extend(
            f"- {_format_session(session, include_branch=False)}"
            for session in cross_project
        )

    return "\n".join(lines)
