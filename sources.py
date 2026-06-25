"""Session source discovery for Claude Code, Pi, and Codex."""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSessionFile:
    source: str
    path: str


def _expand_pi_path(value: str, *, base_dir: str) -> str:
    if value.startswith("~"):
        return os.path.expanduser(value)
    if os.path.isabs(value):
        return value
    return os.path.abspath(os.path.join(base_dir, value))


def get_pi_session_dir(explicit: str | None = None) -> str:
    """Return the Pi session directory, honoring explicit/global settings."""
    pi_agent_dir = os.path.expanduser("~/.pi/agent")
    if explicit:
        return _expand_pi_path(explicit, base_dir=pi_agent_dir)

    settings_path = os.path.join(pi_agent_dir, "settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
        configured = settings.get("sessionDir")
        if isinstance(configured, str) and configured:
            return _expand_pi_path(configured, base_dir=pi_agent_dir)
    except (OSError, json.JSONDecodeError):
        pass

    return os.path.join(pi_agent_dir, "sessions")


def discover_claude_sessions(session_id: str | None = None) -> list[SourceSessionFile]:
    projects_dir = os.path.expanduser("~/.claude/projects")
    if not os.path.exists(projects_dir):
        return []
    if session_id:
        pattern = os.path.join(projects_dir, "*", f"{session_id}.jsonl")
    else:
        pattern = os.path.join(projects_dir, "*", "*.jsonl")
    return [SourceSessionFile("claude", path) for path in sorted(glob.glob(pattern))]


def discover_pi_sessions(
    session_id: str | None = None,
    *,
    session_dir: str | None = None,
) -> list[SourceSessionFile]:
    root = get_pi_session_dir(session_dir)
    if not os.path.exists(root):
        return []

    pattern = os.path.join(root, "**", "*.jsonl")
    matches: list[SourceSessionFile] = []
    for path in sorted(glob.glob(pattern, recursive=True)):
        basename = os.path.basename(path)
        # Nested pi-subagents are stored as .../<parent>/<run-group>/run-N/session.jsonl.
        # They are linked from parent transcripts, not indexed as top-level sessions.
        # events.jsonl files are subagent runner lifecycle logs, not conversations.
        if basename in {"session.jsonl", "events.jsonl"}:
            continue
        if session_id and session_id not in os.path.basename(path):
            # Pi's filename includes the native UUID, but callers may pass a DB id pi:<uuid>.
            wanted = session_id.split(":", 1)[-1]
            if wanted not in os.path.basename(path):
                continue
        matches.append(SourceSessionFile("pi", path))
    return matches


def get_codex_session_roots(
    session_dir: str | None = None,
    archived_dir: str | None = None,
) -> tuple[str, str]:
    codex_home = os.path.expanduser("~/.codex")
    active_root = os.path.expanduser(session_dir) if session_dir else os.path.join(codex_home, "sessions")
    archive_root = os.path.expanduser(archived_dir) if archived_dir else os.path.join(codex_home, "archived_sessions")
    return active_root, archive_root


def discover_codex_sessions(
    session_id: str | None = None,
    *,
    session_dir: str | None = None,
    archived_dir: str | None = None,
) -> list[SourceSessionFile]:
    active_root, archive_root = get_codex_session_roots(session_dir, archived_dir)
    wanted = session_id.split(":", 1)[-1] if session_id else ""

    patterns = []
    if os.path.exists(active_root):
        patterns.append(os.path.join(active_root, "**", "rollout-*.jsonl"))
    if os.path.exists(archive_root):
        patterns.append(os.path.join(archive_root, "**", "rollout-*.jsonl"))

    seen: set[str] = set()
    matches: list[SourceSessionFile] = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern, recursive=True)):
            if wanted and wanted not in os.path.basename(path):
                continue
            if path in seen:
                continue
            seen.add(path)
            matches.append(SourceSessionFile("codex", path))
    return matches


def discover_sessions(
    source: str = "all",
    *,
    session_id: str | None = None,
    pi_session_dir: str | None = None,
    codex_session_dir: str | None = None,
    codex_archived_dir: str | None = None,
) -> list[SourceSessionFile]:
    """Discover session JSONL files for a source: claude, pi, codex, or all."""
    source = source.lower()
    if source not in {"claude", "pi", "codex", "all"}:
        raise ValueError(f"Unsupported source: {source}")

    files: list[SourceSessionFile] = []
    if source in {"claude", "all"}:
        files.extend(discover_claude_sessions(session_id))
    if source in {"pi", "all"}:
        files.extend(discover_pi_sessions(session_id, session_dir=pi_session_dir))
    if source in {"codex", "all"}:
        files.extend(discover_codex_sessions(
            session_id,
            session_dir=codex_session_dir,
            archived_dir=codex_archived_dir,
        ))
    return files
