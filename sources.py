"""Session source discovery for Claude Code and Pi."""

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
        # Nested pi-subagents are stored as .../<parent>/<run-group>/run-N/session.jsonl.
        # They are linked from parent transcripts, not indexed as top-level sessions.
        if os.path.basename(path) == "session.jsonl":
            continue
        if session_id and session_id not in os.path.basename(path):
            # Pi's filename includes the native UUID, but callers may pass a DB id pi:<uuid>.
            wanted = session_id.split(":", 1)[-1]
            if wanted not in os.path.basename(path):
                continue
        matches.append(SourceSessionFile("pi", path))
    return matches


def discover_sessions(
    source: str = "all",
    *,
    session_id: str | None = None,
    pi_session_dir: str | None = None,
) -> list[SourceSessionFile]:
    """Discover session JSONL files for a source: claude, pi, or all."""
    source = source.lower()
    if source not in {"claude", "pi", "all"}:
        raise ValueError(f"Unsupported source: {source}")

    files: list[SourceSessionFile] = []
    if source in {"claude", "all"}:
        files.extend(discover_claude_sessions(session_id))
    if source in {"pi", "all"}:
        files.extend(discover_pi_sessions(session_id, session_dir=pi_session_dir))
    return files
