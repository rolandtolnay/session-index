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
from typing import Any

from parser import ParsedQuestionSelection, ParsedSession, ParsedToolCall, _clean_text, _format_question_answers
from project_identity import set_session_project
from session_identity import canonical_session_id
from codex_items import CodexFormatError, normalize_completed_items

CODEX_SOURCE = "codex"

_CODEX_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_EXIT_CODE_RE = re.compile(r"(?:Process exited with code|Exit code:)\s+(-?\d+)")
_SLUG_CHARS = re.compile(r"[^a-z0-9-]+")

_UI_TITLE_PROMPT_PREFIXES = (
    "generate a concise ui title (",
    "you are a helpful assistant. you will be presented with a user prompt, and your job is "
    "to provide a short title for a task that will be created from that prompt.",
)
_APPROVAL_EVALUATOR_PROMPT_PREFIX = (
    "the following is the codex agent history whose request action you are assessing."
)


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
                entry = json.loads(line)
                if isinstance(entry, dict):
                    entries.append(entry)
            except json.JSONDecodeError:
                continue
    return entries


def _codex_home() -> str:
    return os.path.expanduser(
        os.environ.get("SESSION_INDEX_CODEX_HOME")
        or os.environ.get("CODEX_HOME")
        or "~/.codex"
    )


def _native_id_from_filename(path: str) -> str:
    match = _CODEX_UUID_RE.search(os.path.basename(path))
    return match.group(1) if match else ""


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
    if match:
        return match.group(1) != "0"
    lowered = (output or "").lstrip().lower()
    return lowered.startswith(("script failed", "error:", "failed:"))


def _tool_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    text = _content_text(output)
    if text:
        return text
    try:
        return json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(output)


def _tool_arguments(value: Any) -> dict[str, Any]:
    arguments = _parse_arguments(value)
    if arguments:
        return arguments
    if isinstance(value, str) and value:
        return {"input": value}
    return {}


def _question_outcome(call: ParsedToolCall) -> tuple[list[ParsedQuestionSelection], bool]:
    """Only structured, ID-matched answers are evidence of a Codex user decision."""
    if call.tool_name.rsplit(".", 1)[-1] != "request_user_input":
        return [], False
    result = _parse_arguments(call.result)
    cancelled = result.get("cancelled") is True
    answers = result.get("answers")
    questions = call.arguments.get("questions")
    if cancelled or call.is_error or not isinstance(answers, dict) or not isinstance(questions, list):
        return [], cancelled
    selections = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        identity, text = question.get("id"), question.get("question")
        if not isinstance(identity, str) or not identity or not isinstance(text, str):
            continue
        answer = answers.get(identity)
        labels = answer.get("answers") if isinstance(answer, dict) else None
        # Do not stringify malformed payloads into apparent user input.
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            labels = []
        selections.append(ParsedQuestionSelection(
            question=text, question_id=identity,
            selected_labels=[label for label in labels if label.strip()],
        ))
    return selections, False


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


def _session_meta_payload(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("type") == "session_meta":
                    payload = entry.get("payload")
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        pass
    return {}


def is_codex_subagent(metadata: dict) -> bool:
    if metadata.get("thread_source") == "user":
        return False
    source = metadata.get("source")
    return metadata.get("thread_source") in {"subagent", "guardian_review"} or (
        isinstance(source, dict) and "subagent" in source
    )


def codex_parent_id(metadata: dict) -> str:
    source = metadata.get("source")
    child = source.get("subagent") if isinstance(source, dict) else None
    spawn = child.get("thread_spawn") if isinstance(child, dict) else None
    parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    return parent if isinstance(parent, str) else ""


def discover_codex_subagents(path: str):
    from sources import discover_codex_sessions
    from subagent_parser import SubagentInfo

    meta = _session_meta_payload(path)
    parent = meta.get("id") or meta.get("session_id") or _native_id_from_filename(path)
    candidates = []
    for source in discover_codex_sessions():
        child = _session_meta_payload(source.path)
        owner = codex_parent_id(child)
        if not owner or not is_codex_subagent(child):
            continue
        identity = child.get("id") or child.get("session_id") or _native_id_from_filename(source.path)
        spawn = child["source"]["subagent"]["thread_spawn"]
        candidates.append((owner, SubagentInfo(source.path, None, identity,
                          child.get("agent_role") or spawn.get("agent_role") or "subagent")))
    # Include descendants, even if their parent had no direct spawn tool record.
    owners = {parent}
    results = {}
    while True:
        additions = [info for owner, info in candidates if owner in owners and info.agent_id not in owners]
        if not additions:
            break
        for info in additions:
            owners.add(info.agent_id)
            results[info.agent_id] = info
    return sorted(results.values(), key=lambda info: info.jsonl_path)


def parse_codex_subagent(info):
    from subagent_parser import ParsedSubagent

    entries = _load_jsonl(info.jsonl_path)
    meta = next((e.get("payload", {}) for e in entries if e.get("type") == "session_meta"), {})
    started = meta.get("timestamp", "")
    # Forked child rollouts may carry inherited history. Only the child's own
    # exchanges belong in its transcript; retain metadata for identity.
    if started:
        entries = [e for e in entries if e.get("type") == "session_meta" or _entry_timestamp(e) >= started]
    session = parse_codex_jsonl(info.jsonl_path, entries=entries, enrich_metadata=False)
    return ParsedSubagent(
        agent_id=info.agent_id, agent_type=info.agent_type,
        parent_session_id=session.parent_native_session_id,
        started_at=session.started_at, ended_at=session.ended_at,
        duration_seconds=session.duration_seconds, files_touched=session.files_touched,
        tools_used=session.tools_used, tool_call_count=len(session.tool_calls),
        messages=session.messages, initial_prompt=session.user_messages[0] if session.user_messages else "",
        tool_calls=session.tool_calls, source_path=info.jsonl_path,
    )


def internal_codex_session_reason(session: ParsedSession, path: str = "") -> str:
    """Classify provider-internal rollouts that are not user conversations."""
    if not session.user_messages:
        return ""
    first_user = session.user_messages[0].lstrip().casefold()
    is_title_prompt = first_user.startswith(_UI_TITLE_PROMPT_PREFIXES)
    is_approval_prompt = first_user.startswith(_APPROVAL_EVALUATOR_PROMPT_PREFIX)
    if not is_title_prompt and not is_approval_prompt:
        return ""

    metadata = _session_meta_payload(path) if path else {}
    if metadata.get("thread_source") == "user":
        return ""

    if is_title_prompt:
        if len(session.user_messages) == 1 and metadata.get("source") == "exec":
            return "Codex UI-title side-call"
        return ""

    source = metadata.get("source")
    guardian = (
        source.get("subagent", {}).get("other")
        if isinstance(source, dict) and isinstance(source.get("subagent"), dict)
        else ""
    )
    if metadata.get("thread_source") == "subagent" and guardian == "guardian":
        return "Codex approval-evaluator side-call"
    return ""


def parse_codex_jsonl(
    path: str, *, entries: list[dict[str, Any]] | None = None,
    enrich_metadata: bool = True,
) -> ParsedSession:
    """Parse a rollout; captured sources can opt out of external metadata."""
    session = ParsedSession()

    try:
        entries = _load_jsonl(path) if entries is None else entries
    except OSError:
        return session
    if not entries:
        return session

    raw_user_items = any(
        e.get("type") == "response_item" and isinstance(e.get("payload"), dict)
        and e["payload"].get("role") == "user" for e in entries
    )
    entries = normalize_completed_items(entries)

    native_id = _native_id_from_filename(path)
    meta_cwd = ""
    meta_branch = ""
    meta_started_at = ""
    agent_path = ""
    timestamps: list[str] = []
    tool_outputs: dict[str, dict[str, Any]] = {}
    raw_tool_calls: list[ParsedToolCall] = []
    question_calls: dict[str, ParsedToolCall] = {}
    files_set: set[str] = set()
    tool_counter: Counter[str] = Counter()
    patch_call_ids: set[str] = set()
    task_complete_fallback = ""
    task_complete_ts = ""

    for entry_index, entry in enumerate(entries):
        ts = _entry_timestamp(entry)
        if ts:
            timestamps.append(ts)

        entry_type = entry.get("type")
        payload = entry.get("payload", {})
        payload = payload if isinstance(payload, dict) else {}

        if entry_type == "session_meta":
            # Forks can retain their ancestor's session_id; id is the thread's
            # native identity (and matches the rollout filename).
            native_id = str(payload.get("id") or payload.get("session_id") or native_id)
            session.is_subagent = is_codex_subagent(payload)
            session.parent_native_session_id = codex_parent_id(payload)
            if session.is_subagent:
                source = payload.get("source")
                child = source.get("subagent") if isinstance(source, dict) else None
                spawn = child.get("thread_spawn", {}) if isinstance(child, dict) else {}
                agent_path = payload.get("agent_path") or spawn.get("agent_path") or ""
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

        elif entry_type == "event_msg" and payload.get("type") == "patch_apply_end":
            call_id = payload.get("call_id", "")
            if isinstance(call_id, str) and call_id:
                patch_call_ids.add(call_id)

        elif entry_type == "response_item" and payload.get("type") in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            call_id = payload.get("call_id", "")
            output = payload.get("output", "")
            if isinstance(call_id, str) and call_id:
                content = _tool_output_text(output)
                tool_outputs[call_id] = {
                    "content": content,
                    "is_error": _is_error_output(content),
                    "entry_index": entry_index,
                }

    thread = _thread_metadata(native_id) if enrich_metadata else CodexThreadMetadata()
    if native_id:
        session.native_session_id = native_id
        session.session_id = canonical_session_id(CODEX_SOURCE, native_id)
    if thread.title:
        session.slug = _slugify(thread.title)
    if not session.model and thread.model:
        session.model = thread.model

    cwd = meta_cwd or thread.cwd
    if cwd:
        set_session_project(session, cwd)
    session.branch = meta_branch or thread.git_branch

    for entry_index, entry in enumerate(entries):
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
            if payload_type in {"function_call_output", "custom_tool_call_output"}:
                call_id = payload.get("call_id")
                if not isinstance(call_id, str):
                    continue
                call = question_calls.get(call_id)
                if call and tool_outputs[call_id]["entry_index"] == entry_index:
                    text = _format_question_answers(
                        call.tool_name, call.arguments, "",
                        selections=call.question_selections, cancelled=call.question_cancelled,
                    )
                    if text:
                        session.user_messages.append(text)
                        session.messages.append({"role": "user", "content": text, "timestamp": ts})
            elif payload_type == "agent_message" and session.is_subagent and agent_path and payload.get("recipient") == agent_path:
                text = _clean_text(_content_text(payload.get("content", [])))
                if text:
                    text = f"[Agent message from {payload.get('author', 'parent')}]\n{text}"
                    session.user_messages.append(text)
                    session.messages.append({"role": "user", "content": text, "timestamp": ts})
            elif payload_type == "message":
                role = payload.get("role")
                if role == "assistant":
                    text = _clean_text(_content_text(payload.get("content", [])))
                    if text:
                        session.assistant_messages.append(text)
                        session.messages.append({"role": "assistant", "content": text, "timestamp": ts})

            elif payload_type in {"function_call", "custom_tool_call"}:
                name = payload.get("name", "")
                if not isinstance(name, str) or not name:
                    continue
                call_id = payload.get("call_id", "")
                if (
                    payload_type == "custom_tool_call"
                    and isinstance(call_id, str)
                    and call_id in patch_call_ids
                    and name == "apply_patch"
                ):
                    # patch_apply_end carries the authoritative file/change and
                    # success data for this call.
                    continue
                raw_arguments = payload.get("input") if payload_type == "custom_tool_call" else payload.get("arguments")
                arguments = _tool_arguments(raw_arguments)
                output = tool_outputs.get(call_id if isinstance(call_id, str) else "", {})
                tool_counter[name] += 1
                files_set.update(_top_level_paths(arguments))
                call = ParsedToolCall(
                    timestamp=ts,
                    tool_call_id=call_id if isinstance(call_id, str) else "",
                    tool_name=name,
                    arguments=arguments,
                    result=output.get("content", ""),
                    is_error=bool(output.get("is_error", False) or payload.get("status") == "failed"),
                )
                call.question_selections, call.question_cancelled = _question_outcome(call)
                raw_tool_calls.append(call)
                if isinstance(call_id, str) and call_id and name.rsplit(".", 1)[-1] == "request_user_input":
                    question_calls[call_id] = call

    if not session.assistant_messages and task_complete_fallback:
        session.assistant_messages.append(task_complete_fallback)
        session.messages.append({"role": "assistant", "content": task_complete_fallback, "timestamp": task_complete_ts})

    if not session.slug:
        session.slug = _first_user_slug(session)

    session.started_at = meta_started_at or thread.created_at or (timestamps[0] if timestamps else "")
    session.ended_at = timestamps[-1] if timestamps else thread.updated_at
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
    if raw_user_items and session.assistant_messages and not session.user_messages:
        raise CodexFormatError("Codex has response user items but no supported visible user messages")
    return session
