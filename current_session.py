"""Resolve the active runtime session from Session Index environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

import tool_log
import transcript

ENV_SESSION_ID = "SESSION_INDEX_SESSION_ID"
ENV_NATIVE_SESSION_ID = "SESSION_INDEX_NATIVE_SESSION_ID"
ENV_SOURCE = "SESSION_INDEX_SOURCE"
ENV_SOURCE_PATH = "SESSION_INDEX_SOURCE_PATH"
ENV_LEAF_ID = "SESSION_INDEX_LEAF_ID"

# Claude Code exposes the active session id to Bash tool calls / slash-command
# snippets as CLAUDE_CODE_SESSION_ID (the official, stable name); older/hook-era
# contexts used CLAUDE_SESSION_ID. Accept both, newest convention first.
CLAUDE_ENV_SESSION_IDS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
)
# Claude Code does NOT expose the transcript path as an env var to ordinary Bash
# calls (only hooks receive it, via stdin JSON). When absent we locate the raw
# JSONL by exact session id instead — see _locate_claude_source_path.
CLAUDE_ENV_SOURCE_PATHS = (
    "CLAUDE_TRANSCRIPT_PATH",
    "CLAUDE_CODE_TRANSCRIPT_PATH",
)

REQUIRED_ENV = (
    ENV_SESSION_ID,
    ENV_NATIVE_SESSION_ID,
    ENV_SOURCE,
    ENV_SOURCE_PATH,
)
RESOLUTION_METHOD = "session_index_env"
_ERROR_PREFIX = (
    "current only works inside an active agent runtime exposing Session Index env"
)


class CurrentSessionError(ValueError):
    """Raised when exact current-session environment is unavailable."""


@dataclass(frozen=True)
class CurrentSession:
    """Resolved current-session identity and deterministic artifact paths."""

    session_id: str
    native_session_id: str
    source: str
    source_path: str
    transcript_path: str
    tool_log_path: str
    transcript_exists: bool
    tool_log_exists: bool
    source_path_exists: bool
    transcript_written_at: str | None = None
    tool_log_written_at: str | None = None
    resolution_method: str = RESOLUTION_METHOD
    leaf_id: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        """Return the public JSON representation for the CLI."""
        data: dict[str, object] = {
            "session_id": self.session_id,
            "native_session_id": self.native_session_id,
            "source": self.source,
            "source_path": self.source_path,
            "transcript_path": self.transcript_path,
            "tool_log_path": self.tool_log_path,
            "source_path_exists": self.source_path_exists,
            "transcript_exists": self.transcript_exists,
            "tool_log_exists": self.tool_log_exists,
            "resolution_method": self.resolution_method,
        }
        if self.transcript_written_at:
            data["transcript_written_at"] = self.transcript_written_at
        if self.tool_log_written_at:
            data["tool_log_written_at"] = self.tool_log_written_at
        if self.leaf_id:
            data["leaf_id"] = self.leaf_id
        return data


def _artifact_transcript_path(session_id: str) -> str:
    return os.path.join(transcript.TRANSCRIPT_DIR, f"{session_id}.md")


def _artifact_tool_log_path(session_id: str) -> str:
    return os.path.join(tool_log.TRANSCRIPT_DIR, f"{session_id}.tools.md")


def _required_value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _artifact_written_at(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat()


def _first_required_value(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = _required_value(env, name)
        if value is not None:
            return value
    return None


def _fail(detail: str) -> CurrentSessionError:
    return CurrentSessionError(f"{_ERROR_PREFIX}: {detail}")


def _normalize_identity(source: str, session_id: str, native_session_id: str) -> tuple[str, str]:
    if source == "pi":
        native = native_session_id.removeprefix("pi:")
        canonical = session_id if session_id.startswith("pi:") else f"pi:{session_id}"
        expected = f"pi:{native}"
        if canonical != expected:
            raise _fail(
                "inconsistent SESSION_INDEX_SESSION_ID and "
                "SESSION_INDEX_NATIVE_SESSION_ID for pi source"
            )
        return canonical, native

    if source == "claude":
        if session_id.startswith("pi:") or native_session_id.startswith("pi:"):
            raise _fail("inconsistent pi-prefixed ID for claude source")
        if session_id != native_session_id:
            raise _fail(
                "inconsistent SESSION_INDEX_SESSION_ID and "
                "SESSION_INDEX_NATIVE_SESSION_ID for claude source"
            )
        return session_id, native_session_id

    raise _fail(f"unsupported SESSION_INDEX_SOURCE: {source}")


def _has_public_env(env: Mapping[str, str]) -> bool:
    return any(_required_value(env, name) is not None for name in REQUIRED_ENV)


def _resolve_public_env(env: Mapping[str, str]) -> tuple[str, str, str, str, str | None]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in REQUIRED_ENV:
        value = _required_value(env, name)
        if value is None:
            missing.append(name)
        else:
            values[name] = value

    if missing:
        raise _fail(f"missing required env: {', '.join(missing)}")

    leaf_id = _required_value(env, ENV_LEAF_ID)
    return (
        values[ENV_SESSION_ID],
        values[ENV_NATIVE_SESSION_ID],
        values[ENV_SOURCE].lower(),
        values[ENV_SOURCE_PATH],
        leaf_id,
    )


def _locate_claude_source_path(session_id: str) -> str | None:
    """Locate the raw JSONL for an exact Claude session id.

    Deterministic resolution of a *known* id's file — the same
    ~/.claude/projects/*/<id>.jsonl glob sources.discover_claude_sessions uses,
    not a latest/most-recent guess — so it cannot resolve a different parallel
    session. Used when Claude Code exposes the session id but no transcript path.
    """
    from sources import discover_claude_sessions

    matches = discover_claude_sessions(session_id)
    return matches[0].path if matches else None


def _resolve_claude_compat_env(env: Mapping[str, str]) -> tuple[str, str, str, str, None]:
    session_id = _first_required_value(env, CLAUDE_ENV_SESSION_IDS)
    if session_id is None:
        raise _fail(
            "insufficient claude compatibility env: missing "
            + " or ".join(CLAUDE_ENV_SESSION_IDS)
        )

    # Prefer an explicit source-transcript path; otherwise locate the raw JSONL
    # for this exact session id.
    source_path = _first_required_value(env, CLAUDE_ENV_SOURCE_PATHS)
    if source_path is None:
        source_path = _locate_claude_source_path(session_id)
    if source_path is None:
        raise _fail(
            f"could not locate the source transcript for claude session {session_id!r}: "
            f"set {' or '.join(CLAUDE_ENV_SOURCE_PATHS)}, or ensure "
            f"~/.claude/projects/*/{session_id}.jsonl exists"
        )

    return session_id, session_id, "claude", source_path, None


def _resolve_env_inputs(env: Mapping[str, str]) -> tuple[str, str, str, str, str | None]:
    if _has_public_env(env):
        return _resolve_public_env(env)

    has_claude_compat = (
        _first_required_value(env, CLAUDE_ENV_SESSION_IDS) is not None
        or _first_required_value(env, CLAUDE_ENV_SOURCE_PATHS) is not None
    )
    if has_claude_compat:
        return _resolve_claude_compat_env(env)

    required = ", ".join(REQUIRED_ENV)
    claude_ids = " or ".join(CLAUDE_ENV_SESSION_IDS)
    claude_paths = " or ".join(CLAUDE_ENV_SOURCE_PATHS)
    raise _fail(
        f"missing required env: {required}; claude compatibility requires "
        f"{claude_ids} (plus {claude_paths}, or a discoverable "
        f"~/.claude/projects/*/<session>.jsonl)"
    )


def resolve_current_session(env: Mapping[str, str] | None = None) -> CurrentSession:
    """Resolve the active current session from exact runtime env.

    This intentionally does not query the database, latest sessions, terminal
    state, or any registry. Missing or inconsistent env is reported as an
    explicit failure because guessing can identify the wrong parallel session.
    Session Index's public SESSION_INDEX_* contract takes precedence. Claude's
    native env is accepted via CLAUDE_CODE_SESSION_ID / CLAUDE_SESSION_ID; the
    source transcript path is taken from CLAUDE_(CODE_)TRANSCRIPT_PATH when set,
    otherwise located by the *exact* session id (~/.claude/projects/*/<id>.jsonl),
    which is deterministic for a known id and so cannot resolve a different session.
    """
    env = os.environ if env is None else env

    session_id, native_session_id, source, source_path, leaf_id = _resolve_env_inputs(env)
    canonical, native = _normalize_identity(source, session_id, native_session_id)
    transcript_path = _artifact_transcript_path(canonical)
    tool_log_path = _artifact_tool_log_path(canonical)

    return CurrentSession(
        session_id=canonical,
        native_session_id=native,
        source=source,
        source_path=source_path,
        transcript_path=transcript_path,
        tool_log_path=tool_log_path,
        source_path_exists=os.path.exists(source_path),
        transcript_exists=os.path.exists(transcript_path),
        tool_log_exists=os.path.exists(tool_log_path),
        transcript_written_at=_artifact_written_at(transcript_path),
        tool_log_written_at=_artifact_written_at(tool_log_path),
        leaf_id=leaf_id if source == "pi" else None,
    )
