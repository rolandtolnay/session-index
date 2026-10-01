"""Synchronous, read-only Clean Transcript snapshots for exact commit origins."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from current_session import (
    CLAUDE_ENV_SESSION_IDS, CLAUDE_ENV_SOURCE_PATHS, CurrentSessionError,
    _has_public_env, _normalize_identity, _resolve_alias_value, _resolve_env_inputs,
)
from session_identity import canonical_session_id, is_canonical_session_id
from transcript import render_transcript

MAX_SOURCE_BYTES = 64 * 1024 * 1024


class SnapshotError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def to_json(self) -> dict:
        return {"error": {"code": self.code, "message": str(self)}}


def _runtime_inputs(env: Mapping[str, str]) -> tuple[str, str, str, str, str | None]:
    # Reuse current's exact identity contract, but never rank duplicate Claude
    # files by parsing them or inspect generated artifact paths.
    env = dict(env)
    if not _has_public_env(env):
        native = _resolve_alias_value(env, CLAUDE_ENV_SESSION_IDS, "session id")
        path = _resolve_alias_value(env, CLAUDE_ENV_SOURCE_PATHS, "transcript path")
        thread_id = env.get("CODEX_THREAD_ID", "").strip()
        if thread_id:
            if native or path:
                raise SnapshotError("invalid_identity", "Conflicting claude and codex compatibility env")
            canonical = canonical_session_id("codex", thread_id)
            home = os.path.expanduser(env.get("SESSION_INDEX_CODEX_HOME") or env.get("CODEX_HOME") or "~/.codex")
            paths = sorted({
                path
                for directory in ("sessions", "archived_sessions")
                for path in glob.glob(os.path.join(home, directory, "**", f"rollout-*-{glob.escape(thread_id)}.jsonl"), recursive=True)
            })
            if len(paths) != 1:
                raise SnapshotError("source_unavailable", "Expected exactly one Codex rollout for CODEX_THREAD_ID; supply explicit source identity")
            return canonical, thread_id, "codex", paths[0], None
        if native and not path:
            canonical_session_id("claude", native)  # validate before globbing
            paths = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{glob.escape(native)}.jsonl"))
            if len(paths) != 1:
                raise SnapshotError("source_unavailable", "Expected exactly one Claude source; set CLAUDE_TRANSCRIPT_PATH explicitly")
            env[CLAUDE_ENV_SOURCE_PATHS[0]] = paths[0]
    return _resolve_env_inputs(env)


def _indexed_inputs(session_id: str, db_path: str | None) -> tuple[str, str, str, str, None]:
    if not is_canonical_session_id(session_id):
        raise SnapshotError("invalid_identity", "--session requires an exact canonical Session ID (no native IDs or prefixes)")
    if db_path is None:
        from db import DB_PATH
        db_path = DB_PATH
    try:
        conn = sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT native_session_id, source, source_path FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SnapshotError("source_unavailable", f"Cannot read indexed session identity: {exc}") from exc
    if row is None:
        raise SnapshotError("unknown_session", f"No indexed session with exact ID {session_id!r}")
    native, source, path = row
    if not native or not source or not path:
        raise SnapshotError("source_unavailable", "Indexed session lacks source identity/path; supply explicit source identity")
    return session_id, native, source, path, None


def _git(cwd: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError("invalid_worktree", f"Cannot inspect Git worktree: {exc}") from exc
    if result.returncode:
        raise SnapshotError("invalid_worktree", f"Not an accessible Git worktree: {cwd!r}")
    return result.stdout.strip()


def _worktree_root(cwd: str) -> str:
    if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
        raise SnapshotError("invalid_worktree", "Source and --cwd must provide absolute Git worktree paths")
    return os.path.realpath(_git(cwd, "rev-parse", "--show-toplevel"))


def _capture(path: str) -> tuple[bytes, list[dict], str]:
    try:
        # Reject final symlinks; the caller guards raw and resolved parent paths.
        # Nonblocking open allows rejecting FIFOs/devices without hanging.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SnapshotError("invalid_source", "Source must be a regular JSONL file")
            if before.st_size > MAX_SOURCE_BYTES:
                raise SnapshotError("source_too_large", f"Source exceeds {MAX_SOURCE_BYTES} bytes")
            data = handle.read(before.st_size)
            after = os.fstat(handle.fileno())
            if len(data) != before.st_size or after.st_size < before.st_size or (
                after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns
            ):
                raise SnapshotError("source_changed", "Source changed during capture; retry snapshot")
    except OSError as exc:
        raise SnapshotError("source_unavailable", f"Cannot read source transcript: {exc}") from exc
    captured_at = datetime.now(timezone.utc).isoformat()
    if not data or not data.endswith(b"\n"):
        raise SnapshotError("invalid_source", "Source snapshot is empty or lacks a complete final JSONL line; retry after the provider finishes writing")
    try:
        text = data.decode("utf-8")
        entries = []
        for number, line in enumerate(text.split("\n"), 1):
            if not line.strip():
                continue
            entry = json.loads(line)
            # JSON may encode lone surrogates which are not UTF-8 transcript text.
            json.dumps(entry, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if not isinstance(entry, dict):
                raise SnapshotError("invalid_source", f"Source line {number} is not a JSON object")
            entries.append(entry)
    except ValueError as exc:
        if isinstance(exc, SnapshotError):
            raise
        raise SnapshotError("invalid_source", f"Invalid/incomplete UTF-8 JSONL source snapshot: {exc}") from exc
    return data, entries, captured_at


def _source_metadata(source: str, entries: list[dict]) -> tuple[set[str], list[str]]:
    identities: set[str] = set()
    cwds: list[str] = []
    for entry in entries:
        if source == "pi":
            metadata = entry if entry.get("type") == "session" else {}
            native = metadata.get("id")
        elif source == "claude":
            metadata = entry
            native = metadata.get("sessionId")
        else:
            metadata = entry.get("payload", {}) if entry.get("type") in {"session_meta", "turn_context"} else {}
            if not isinstance(metadata, dict):
                raise SnapshotError("invalid_source", "Invalid Codex source metadata")
            native = (metadata.get("session_id") or metadata.get("id")) if entry.get("type") == "session_meta" else None
        if native is not None:
            if not isinstance(native, str) or not native:
                raise SnapshotError("invalid_source", "Invalid native identity in source")
            identities.add(native)
        cwd = metadata.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd:
                raise SnapshotError("invalid_source", "Invalid cwd in source")
            cwds.append(cwd)
    return identities, cwds


def _validate_messages(source: str, entries: list[dict]) -> None:
    """Reject malformed conversation records rather than silently lose a correction."""
    for entry in entries:
        message = None
        if source == "claude" and entry.get("type") in {"user", "assistant"}:
            message = entry.get("message")
        elif source == "pi" and entry.get("type") == "message":
            message = entry.get("message")
            # SDK shell/summary messages and host-added roles need not have content.
            # They are not user/assistant corrections consumed by the cleaner.
            if isinstance(message, dict) and isinstance(message.get("role"), str) and message["role"] not in {"user", "assistant", "toolResult", "system", "custom"}:
                continue
        elif source == "codex" and entry.get("type") in {"event_msg", "response_item"}:
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                raise SnapshotError("invalid_source", "Invalid Codex event payload")
            if payload.get("type") == "user_message" and not isinstance(payload.get("message"), str):
                raise SnapshotError("invalid_source", "Invalid Codex user message")
            if payload.get("type") == "message":
                message = payload
        else:
            continue
        if message is not None or source != "codex":
            if not isinstance(message, dict) or not isinstance(message.get("content"), (str, list)):
                raise SnapshotError("invalid_source", "Conversation record has missing/invalid message content")
            content = message["content"]
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, str) and source == "claude":
                        continue
                    if not isinstance(block, dict):
                        raise SnapshotError("invalid_source", "Invalid conversation content block")
                    if block.get("type") in {"text", "input_text", "output_text"} and not isinstance(block.get("text"), str):
                        raise SnapshotError("invalid_source", "Invalid conversation text block")


def resolve_snapshot_origin(
    *, max_chars: int = 80000, session: str | None = None,
    source: str | None = None, source_path: str | None = None,
    native_session_id: str | None = None, leaf_id: str | None = None,
    env: Mapping[str, str] | None = None, db_path: str | None = None,
) -> dict:
    """Resolve exact identity without opening the source, for caller security guards."""
    if max_chars <= 0:
        raise SnapshotError("invalid_arguments", "--max-chars must be positive")
    explicit = (source, source_path, native_session_id)
    if session is not None and any(value is not None for value in explicit):
        raise SnapshotError("invalid_arguments", "--session conflicts with explicit source identity flags")
    try:
        if session is not None:
            canonical, native, provider, path, resolved_leaf = _indexed_inputs(session, db_path)
        elif any(value is not None for value in explicit):
            if not all(explicit):
                raise SnapshotError("invalid_arguments", "Supply --source, --source-path and --native-session-id together")
            if source not in {"claude", "pi", "codex"}:
                raise SnapshotError("invalid_identity", f"Unsupported source: {source!r}")
            canonical, native, provider, path, resolved_leaf = canonical_session_id(source, native_session_id), native_session_id, source, source_path, None
        else:
            canonical, native, provider, path, resolved_leaf = _runtime_inputs(os.environ if env is None else env)
        _normalize_identity(provider, canonical, native)
    except (CurrentSessionError, KeyError, ValueError) as exc:
        if isinstance(exc, SnapshotError):
            raise
        raise SnapshotError("invalid_identity", str(exc)) from exc
    if leaf_id is not None and (not leaf_id.strip() or (resolved_leaf is not None and leaf_id != resolved_leaf)):
        raise SnapshotError("invalid_arguments", "Empty or conflicting Pi leaf identity")
    leaf = leaf_id if leaf_id is not None else resolved_leaf
    if provider == "pi" and not leaf:
        raise SnapshotError("missing_leaf", "Pi snapshot requires --leaf-id from getLeafId() or SESSION_INDEX_LEAF_ID; no latest-branch guessing")
    if provider != "pi" and leaf is not None:
        raise SnapshotError("invalid_arguments", "--leaf-id is only supported for Pi")
    return {
        "session_id": canonical, "native_session_id": native, "source": provider,
        "source_path": os.path.abspath(os.path.expanduser(path)), "leaf_id": leaf,
    }


def capture_snapshot(
    *, cwd: str, max_chars: int = 80000, session: str | None = None,
    source: str | None = None, source_path: str | None = None,
    native_session_id: str | None = None, leaf_id: str | None = None,
    env: Mapping[str, str] | None = None, db_path: str | None = None,
    resolve_only: bool = False,
) -> dict:
    """Resolve or capture one exact origin, without indexing, artifacts or LLM work."""
    origin = resolve_snapshot_origin(
        max_chars=max_chars, session=session, source=source, source_path=source_path,
        native_session_id=native_session_id, leaf_id=leaf_id, env=env, db_path=db_path,
    )
    if resolve_only:
        return origin
    canonical, native = origin["session_id"], origin["native_session_id"]
    provider, path, leaf = origin["source"], origin["source_path"], origin["leaf_id"]
    repo_root = _worktree_root(cwd)
    data, entries, captured_at = _capture(path)
    identities, cwds = _source_metadata(provider, entries)
    if identities != {native}:
        raise SnapshotError("identity_mismatch", "Source native identity does not match requested session")
    if not cwds:
        raise SnapshotError("invalid_worktree", "Source has no cwd to establish its exact Git worktree")
    if any(_worktree_root(source_cwd) != repo_root for source_cwd in set(cwds)):
        raise SnapshotError("worktree_mismatch", "Source belongs to a different Git worktree than --cwd")
    _validate_messages(provider, entries)
    try:
        if provider == "pi":
            from pi_parser import parse_pi_jsonl
            parsed = parse_pi_jsonl(path, entries=entries, leaf_id=leaf, include_tool_errors=False)
        elif provider == "claude":
            from parser import parse_jsonl
            parsed = parse_jsonl(path, entries=entries, include_tool_errors=False)
        else:
            from codex_parser import parse_codex_jsonl
            parsed = parse_codex_jsonl(path, entries=entries, enrich_metadata=False)
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise SnapshotError("invalid_source", f"Cannot parse exact source snapshot: {exc}") from exc
    if parsed.native_session_id != native:
        raise SnapshotError("identity_mismatch", "Parsed source identity does not match requested session")
    if not parsed.user_messages or not parsed.assistant_messages:
        raise SnapshotError("empty_conversation", "Snapshot requires nonempty user and assistant conversation")
    branch = _git(repo_root, "branch", "--show-current")
    rendered = render_transcript(parsed.messages, project=os.path.basename(repo_root), branch=branch, timestamp=parsed.started_at)
    if len(rendered) > max_chars:
        raise SnapshotError("context_too_large", f"Clean Transcript has {len(rendered)} characters, exceeding --max-chars {max_chars}; select a narrower exact origin or explicitly raise the limit")
    return {
        "version": 1, "session_id": canonical, "native_session_id": native,
        "source": provider, "source_path": path, "leaf_id": leaf,
        "repo_root": repo_root, "branch": branch, "captured_at": captured_at,
        "source_sha256": hashlib.sha256(data).hexdigest(), "source_bytes": len(data),
        "transcript_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "transcript": rendered,
    }
