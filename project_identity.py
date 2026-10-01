"""Repository-level project identity, separate from a conversation's location."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from parser import ParsedSession


@dataclass(frozen=True)
class ProjectIdentity:
    project_id: str
    project_path: str
    project: str
    cwd: str
    worktree_path: str | None = None


def normalized_path(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def resolve_project(cwd: str) -> ProjectIdentity:
    """Use Git's shared repository directory, never a name/remote heuristic.

    A missing or non-Git folder remains its own project. Callers indexing an
    existing session restore its persisted identity if Git is no longer there.
    Git environment overrides must not redirect a lookup to the agent's repo.
    """
    folder = normalized_path(cwd)
    fallback = ProjectIdentity(f"dir:{folder}", folder, os.path.basename(folder), cwd)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
            cwd=folder, env=env, capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().splitlines()
        if result.returncode or len(lines) != 2:
            return fallback
        worktree, common = map(normalized_path, lines)
        # For conventional repositories this is the main checkout. Unusual
        # bare/separate-git-dir layouts use the shared directory itself: unlike
        # an arbitrary checkout, it is the same from every worktree.
        root = os.path.dirname(common) if os.path.basename(common) == ".git" else common
        return ProjectIdentity(f"git:{common}", root, os.path.basename(root), cwd, worktree)
    except (OSError, subprocess.TimeoutExpired):
        return fallback


def set_session_project(session: ParsedSession, cwd: str) -> None:
    identity = resolve_project(cwd)
    for field in ("project_id", "project_path", "project", "cwd", "worktree_path"):
        setattr(session, field, getattr(identity, field) or "")


def restore_project_identity(conn: sqlite3.Connection, session: ParsedSession) -> None:
    """A resolved session's membership outlives its checkout.

    Do this before rendering/summarizing as well as at persistence: otherwise a
    reparse of a removed checkout would leak its folder name into new artifacts.
    """
    row = conn.execute(
        "SELECT project_id, project_path, project, cwd, worktree_path FROM sessions WHERE session_id = ?",
        (session.session_id,),
    ).fetchone()
    if row and row[0] and (row[0].startswith("git:") or not session.project_id.startswith("git:")):
        session.project_id, session.project_path, session.project = row[:3]
        session.cwd = session.cwd or row[3] or ""
        session.worktree_path = session.worktree_path or row[4] or ""
