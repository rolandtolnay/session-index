"""Codex rollout JSONL parser.

Parses Codex Desktop/CLI rollout logs into the same ParsedSession shape used by
the rest of session-index. Codex records visible user/assistant conversation
events separately from model response items and stores patch application results
as event messages, so this parser keeps that provider-specific logic isolated.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from parser import ParsedSession, ParsedToolCall, _clean_text, _git_root

CODEX_SOURCE = "codex"
CODEX_SESSION_PREFIX = "codex:"

_CODEX_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_EXIT_CODE_RE = re.compile(r"(?:Process exited with code|Exit code:)\s+(-?\d+)")
_SLUG_CHARS = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class CodexThreadMetadata:
    title: str = ""
    cwd: str = ""
    git_branch: str = ""
    model: str = ""
    created_at: str = ""
    updated_at: str = ""


def _load_jsonl(path: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _codex_home() -> str:
    return os.path.expanduser(os.environ.get("SESSION_INDEX_CODEX_HOME", "~/.codex"))


def _native_id_from_filename(path: str) -> str:
    match = _CODEX_UUID_RE.search(os.path.basename(path))
    return match.group(1) if match else ""


def _prefixed_session_id(native_id: str) -> str:
    return f"{CODEX_SESSION_PREFIX}{native_id}" if native_id else ""


def _slugify(value: str) -> str:
    slug = value.strip().lower().replace("_", "-").replace(" ", "-")
    slug = _SLUG_CHARS.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:80]


def _iso_from_epoch(value: Any, *, milliseconds: bool = False) -> str:
    if not isinstance(value, (int, float)):
        return ""
    try:
        seconds = value / 1000 if milliseconds else value
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return ""


@lru_cache(maxsize=4)
def _session_index_titles(codex_home: str) -> dict[str, str]:
    titles: dict[str, str] = {}
    path = os.path.join(codex_home, "session_index.jsonl")
    try:
        with open(path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                native_id = row.get("id")
                title = row.get("thread_name")
                if isinstance(native_id, str) and isinstance(title, str) and title.strip():
                    titles[native_id] = title.strip()
    except OSError:
        pass
    return titles


@lru_cache(maxsize=4)
def _state_thread_metadata(codex_home: str) -> dict[str, CodexThreadMetadata]:
    db_path = os.path.join(codex_home, "state_5.sqlite")
    if not os.path.exists(db_path):
        return {}

    rows: dict[str, CodexThreadMetadata] = {}
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT id, title, cwd, git_branch, model,
                   created_at, created_at_ms, updated_at, updated_at_ms
            FROM threads
            """
        )
        for row in cursor.fetchall():
            native_id = row["id"]
            if not isinstance(native_id, str) or not native_id:
                continue
            created_at = _iso_from_epoch(row["created_at_ms"], milliseconds=True) or _iso_from_epoch(row["created_at"])
            updated_at = _iso_from_epoch(row["updated_at_ms"], milliseconds=True) or _iso_from_epoch(row["updated_at"])
            rows[native_id] = CodexThreadMetadata(
                title=row["title"] or "",
                cwd=row["cwd"] or "",
                git_branch=row["git_branch"] or "",
                model=row["model"] or "",
                created_at=created_at,
                updated_at=updated_at,
            )
    except (OSError, sqlite3.Error):
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return rows


def _thread_metadata(native_id: str) -> CodexThreadMetadata:
    if not native_id:
        return CodexThreadMetadata()
    home = _codex_home()
    metadata = _state_thread_metadata(home).get(native_id, CodexThreadMetadata())
    if metadata.title:
        return metadata
    title = _session_index_titles(home).get(native_id, "")
    if title:
        return CodexThreadMetadata(
            title=title,
            cwd=metadata.cwd,
            git_branch=metadata.git_branch,
            model=metadata.model,
            created_at=metadata.created_at,
            updated_at=metadata.updated_at,
        )
    return metadata


def _entry_timestamp(entry: dict[str, Any]) -> str:
    ts = entry.get("timestamp", "")
    return ts if isinstance(ts, str) else ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(p for p in parts if p)


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_error_output(output: str) -> bool:
    match = _EXIT_CODE_RE.search(output or "")
    return bool(match and match.group(1) != "0")


def _top_level_paths(arguments: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "path"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
    return paths


def _patch_change_records(changes: Any) -> list[dict[str, str]]:
    if not isinstance(changes, dict):
        return []

    records: list[dict[str, str]] = []
    for path, change in changes.items():
        if not isinstance(path, str) or not path:
            continue
        record = {"path": path}
        if isinstance(change, dict):
            change_type = change.get("type")
            move_path = change.get("move_path")
            if isinstance(change_type, str) and change_type:
                record["type"] = change_type
            if isinstance(move_path, str) and move_path:
                record["move_path"] = move_path
        records.append(record)
    return records


def _patch_paths(records: list[dict[str, str]]) -> list[str]:
    paths: list[str] = []
    for record in records:
        path = record.get("path", "")
        move_path = record.get("move_path", "")
        if path:
            paths.append(path)
        if move_path:
            paths.append(move_path)
    return paths


def _unique_sorted(values: list[str]) -> list[str]:
    return sorted({value for value in values if value})


def _first_user_slug(session: ParsedSession) -> str:
    if not session.user_messages:
        return ""
    return _slugify(session.user_messages[0])


def parse_codex_jsonl(path: str) -> ParsedSession:
    """Parse a Codex rollout JSONL file into a ParsedSession."""
    session = ParsedSession()

    try:
        entries = _load_jsonl(path)
    except OSError:
        return session
    if not entries:
        return session

    native_id = _native_id_from_filename(path)
    meta_cwd = ""
    meta_branch = ""
    meta_started_at = ""
    timestamps: list[str] = []
    tool_outputs: dict[str, dict[str, Any]] = {}
    raw_tool_calls: list[ParsedToolCall] = []
    files_set: set[str] = set()
    tool_counter: Counter[str] = Counter()
    task_complete_fallback = ""
    task_complete_ts = ""

    for entry in entries:
        ts = _entry_timestamp(entry)
        if ts:
            timestamps.append(ts)

        entry_type = entry.get("type")
        payload = entry.get("payload", {})
        payload = payload if isinstance(payload, dict) else {}

        if entry_type == "session_meta":
            native_id = native_id or str(payload.get("session_id") or payload.get("id") or "")
            if not meta_started_at and isinstance(payload.get("timestamp"), str):
                meta_started_at = payload["timestamp"]
            if not meta_cwd and isinstance(payload.get("cwd"), str):
                meta_cwd = payload["cwd"]
            git = payload.get("git")
            if isinstance(git, dict) and not meta_branch and isinstance(git.get("branch"), str):
                meta_branch = git["branch"]

        elif entry_type == "turn_context":
            if not meta_cwd and isinstance(payload.get("cwd"), str):
                meta_cwd = payload["cwd"]
            if not session.model and isinstance(payload.get("model"), str):
                session.model = payload["model"]

        elif entry_type == "response_item" and payload.get("type") == "function_call_output":
            call_id = payload.get("call_id", "")
            output = payload.get("output", "")
            if isinstance(call_id, str) and call_id:
                tool_outputs[call_id] = {
                    "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
                    "is_error": _is_error_output(output if isinstance(output, str) else ""),
                }

    thread = _thread_metadata(native_id)
    session.session_id = _prefixed_session_id(native_id)
    if thread.title:
        session.slug = _slugify(thread.title)
    if not session.model and thread.model:
        session.model = thread.model

    cwd = meta_cwd or thread.cwd
    if cwd:
        session.project_path = _git_root(cwd)
        session.project = os.path.basename(session.project_path)
    session.branch = meta_branch or thread.git_branch

    for entry in entries:
        ts = _entry_timestamp(entry)
        entry_type = entry.get("type")
        payload = entry.get("payload", {})
        payload = payload if isinstance(payload, dict) else {}

        if entry_type == "event_msg":
            payload_type = payload.get("type")
            if payload_type == "user_message":
                raw_text = payload.get("message", "")
                text = _clean_text(raw_text if isinstance(raw_text, str) else "")
                if text:
                    session.user_messages.append(text)
                    session.messages.append({"role": "user", "content": text, "timestamp": ts})

            elif payload_type == "task_complete":
                message = payload.get("last_agent_message", "")
                if isinstance(message, str) and message.strip():
                    task_complete_fallback = _clean_text(message)
                    task_complete_ts = ts

            elif payload_type == "patch_apply_end":
                call_id = payload.get("call_id", "")
                changes = _patch_change_records(payload.get("changes"))
                arguments = {
                    "changes": changes,
                    "status": payload.get("status", ""),
                    "success": bool(payload.get("success", False)),
                }
                result = "\n".join(
                    p for p in (payload.get("stdout", ""), payload.get("stderr", ""))
                    if isinstance(p, str) and p
                )
                name = "apply_patch"
                tool_counter[name] += 1
                files_set.update(_patch_paths(changes))
                raw_tool_calls.append(ParsedToolCall(
                    timestamp=ts,
                    tool_call_id=call_id if isinstance(call_id, str) else "",
                    tool_name=name,
                    arguments=arguments,
                    result=result,
                    is_error=not bool(payload.get("success", False)),
                ))

        elif entry_type == "response_item":
            payload_type = payload.get("type")
            if payload_type == "message":
                role = payload.get("role")
                if role == "assistant":
                    text = _clean_text(_content_text(payload.get("content", [])))
                    if text:
                        session.assistant_messages.append(text)
                        session.messages.append({"role": "assistant", "content": text, "timestamp": ts})

            elif payload_type == "function_call":
                name = payload.get("name", "")
                if not isinstance(name, str) or not name:
                    continue
                call_id = payload.get("call_id", "")
                arguments = _parse_arguments(payload.get("arguments"))
                output = tool_outputs.get(call_id if isinstance(call_id, str) else "", {})
                tool_counter[name] += 1
                files_set.update(_top_level_paths(arguments))
                raw_tool_calls.append(ParsedToolCall(
                    timestamp=ts,
                    tool_call_id=call_id if isinstance(call_id, str) else "",
                    tool_name=name,
                    arguments=arguments,
                    result=output.get("content", ""),
                    is_error=bool(output.get("is_error", False)),
                ))

    if not session.assistant_messages and task_complete_fallback:
        session.assistant_messages.append(task_complete_fallback)
        session.messages.append({"role": "assistant", "content": task_complete_fallback, "timestamp": task_complete_ts})

    if not session.slug:
        session.slug = _first_user_slug(session)

    session.started_at = meta_started_at or thread.created_at or (timestamps[0] if timestamps else "")
    session.ended_at = thread.updated_at or (timestamps[-1] if timestamps else "")
    if session.started_at and session.ended_at:
        try:
            t0 = datetime.fromisoformat(session.started_at.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(session.ended_at.replace("Z", "+00:00"))
            session.duration_seconds = max(0, int((t1 - t0).total_seconds()))
        except Exception:
            pass

    session.files_touched = _unique_sorted(list(files_set))
    if tool_counter:
        session.tools_used = ", ".join(
            f"{name}:{count}" for name, count in tool_counter.most_common()
        )
    session.tool_calls = raw_tool_calls
    session.user_message_count = len(session.user_messages)
    session.assistant_message_count = len(session.assistant_messages)
    return session
