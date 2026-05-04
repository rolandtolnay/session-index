"""Pi session JSONL parser.

Parses Pi's tree-structured JSONL sessions into the same ParsedSession shape used
by the rest of session-index. The parser indexes the active/latest branch rather
than blindly linearizing all entries, because Pi keeps alternate branches in the
same file.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from parser import ParsedSession, _clean_text, _format_bash_result, _git_root, _strip_narration
from subagent_parser import ParsedSubagent, SubagentInfo

PI_SOURCE = "pi"
PI_SESSION_PREFIX = "pi:"

_NOISE_COMMANDS = {
    "/clear", "/exit", "/compact", "/resume", "/new", "/fork", "/clone",
    "/tree", "/init", "/login", "/logout", "/status", "/config",
    "/help", "/model", "/settings", "/session", "/copy", "/export",
    "/share", "/reload", "/hotkeys", "/changelog", "/quit",
}

_SLUG_CHARS = re.compile(r"[^a-z0-9-]+")
_PI_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.IGNORECASE)


@dataclass
class PiParsedFile:
    """Raw Pi file data after JSONL load and active-branch selection."""

    header: dict[str, Any]
    entries: list[dict[str, Any]]
    branch: list[dict[str, Any]]


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


def _select_active_branch(entries: list[dict[str, Any]]) -> PiParsedFile:
    """Return header and latest branch entries in root-to-leaf order."""
    header = entries[0] if entries and entries[0].get("type") == "session" else {}
    body = [e for e in entries if e.get("type") != "session"]
    by_id = {e.get("id"): e for e in body if e.get("id")}

    leaf_id = ""
    for entry in reversed(body):
        if entry.get("id"):
            leaf_id = entry["id"]
            break

    branch_rev: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = leaf_id
    while current and current in by_id and current not in seen:
        seen.add(current)
        entry = by_id[current]
        branch_rev.append(entry)
        current = entry.get("parentId")

    # If parent links are missing or malformed, fall back to chronological body.
    branch = list(reversed(branch_rev)) if branch_rev else body
    return PiParsedFile(header=header, entries=body, branch=branch)


def _prefixed_session_id(native_id: str) -> str:
    return f"{PI_SESSION_PREFIX}{native_id}" if native_id else ""


def _parent_native_session_id(parent_session: str) -> str:
    """Extract a Pi native session id from a parentSession file reference."""
    if not parent_session:
        return ""
    parent_session = parent_session.split(PI_SESSION_PREFIX, 1)[-1]
    match = _PI_UUID_RE.search(parent_session)
    return match.group(1) if match else ""


def _slugify(name: str) -> str:
    slug = name.strip().lower().replace("_", "-").replace(" ", "-")
    slug = _SLUG_CHARS.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:80]


def _git_branch(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _iso_from_ms(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    try:
        return datetime.utcfromtimestamp(value / 1000).isoformat(timespec="milliseconds") + "Z"
    except Exception:
        return ""


def _entry_timestamp(entry: dict[str, Any]) -> str:
    ts = entry.get("timestamp", "")
    if isinstance(ts, str):
        return ts
    return ""


def _message_timestamp(entry: dict[str, Any], msg: dict[str, Any]) -> str:
    return _entry_timestamp(entry) or _iso_from_ms(msg.get("timestamp"))


def _extract_text_blocks(content: Any, *, include_image_markers: bool = False) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            parts.append(item.get("text", ""))
        elif item_type == "image" and include_image_markers:
            mime = item.get("mimeType") or item.get("mediaType") or "image"
            parts.append(f"[{mime} omitted]")
    return "\n".join(p for p in parts if p)


def _tool_args(block: dict[str, Any]) -> dict[str, Any]:
    args = block.get("arguments", {})
    return args if isinstance(args, dict) else {}


def _tool_file_paths(name: str, args: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    if name in {"read", "write", "edit", "ls", "grep", "find"}:
        path = args.get("path")
        if isinstance(path, str) and path:
            paths.append(path)
    if name == "edit":
        for edit in args.get("edits", []) if isinstance(args.get("edits"), list) else []:
            if isinstance(edit, dict):
                path = edit.get("path")
                if isinstance(path, str) and path:
                    paths.append(path)
    return paths


def _format_tool_signature(name: str, args: dict[str, Any]) -> str:
    if name in {"read", "write", "edit", "ls"}:
        path = args.get("path", "")
        return f"→ {name}: {path}" if path else f"→ {name}"
    if name == "bash":
        cmd = args.get("command", "")
        if isinstance(cmd, str) and len(cmd) > 120:
            cmd = cmd[:120] + "…"
        return f"→ bash: {cmd}" if cmd else "→ bash"
    if name in {"grep", "find"}:
        pattern = args.get("pattern", "")
        path = args.get("path", "")
        if path and pattern:
            return f"→ {name}: {pattern} in {path}"
        return f"→ {name}: {pattern}" if pattern else f"→ {name}"
    if name == "subagent":
        return f"→ subagent: {_subagent_description(args)}"
    return f"→ {name}" if name else "→ tool"


def _subagent_description(args: dict[str, Any]) -> str:
    agent = args.get("agent")
    task = args.get("task")
    if isinstance(agent, str) and isinstance(task, str) and task.strip():
        first = task.strip().splitlines()[0]
        if len(first) > 120:
            first = first[:120] + "…"
        return f"{agent}: {first}"

    tasks = args.get("tasks")
    if isinstance(tasks, list) and tasks:
        labels = []
        for item in tasks[:3]:
            if isinstance(item, dict) and item.get("agent"):
                labels.append(str(item["agent"]))
        suffix = f" ({', '.join(labels)})" if labels else ""
        return f"{len(tasks)} parallel task{'s' if len(tasks) != 1 else ''}{suffix}"

    action = args.get("action")
    if isinstance(action, str) and action:
        return action
    return "subagent task"


def _subagent_marker(args: dict[str, Any]) -> str:
    agent_type = args.get("agent") if isinstance(args.get("agent"), str) else "subagent"
    return f"__SUBAGENT:{agent_type}:{_subagent_description(args)}__"


def _is_noise_command(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    first = stripped.split(maxsplit=1)[0]
    return first in _NOISE_COMMANDS


def _collect_tool_result_text(message: dict[str, Any]) -> str:
    return _extract_text_blocks(message.get("content", ""), include_image_markers=False)


def parse_pi_jsonl(path: str) -> ParsedSession:
    """Parse a Pi JSONL session file into a ParsedSession."""
    session = ParsedSession()

    try:
        parsed = _select_active_branch(_load_jsonl(path))
    except OSError:
        return session

    header = parsed.header
    branch = parsed.branch
    native_id = header.get("id", "")
    if isinstance(native_id, str):
        session.session_id = _prefixed_session_id(native_id)
    parent_session = header.get("parentSession", "")
    if isinstance(parent_session, str) and parent_session:
        session.parent_session_path = parent_session
        session.parent_native_session_id = _parent_native_session_id(parent_session)
    cwd = header.get("cwd", "")
    if isinstance(cwd, str) and cwd:
        session.project_path = _git_root(cwd)
        session.project = os.path.basename(session.project_path)
        session.branch = _git_branch(cwd)

    if isinstance(header.get("timestamp"), str):
        session.started_at = header["timestamp"]

    timestamps = [_entry_timestamp(e) for e in branch if _entry_timestamp(e)]
    if timestamps:
        session.started_at = session.started_at or timestamps[0]
        session.ended_at = timestamps[-1]
        try:
            t0 = datetime.fromisoformat(session.started_at.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(session.ended_at.replace("Z", "+00:00"))
            session.duration_seconds = max(0, int((t1 - t0).total_seconds()))
        except Exception:
            pass

    # First pass: metadata, tool counts/results, files.
    tool_results: dict[str, dict[str, Any]] = {}
    files_set: set[str] = set()
    tool_counter: Counter[str] = Counter()

    for entry in branch:
        entry_type = entry.get("type")
        if entry_type == "session_info":
            name = entry.get("name", "")
            if isinstance(name, str) and name and not session.slug:
                session.slug = _slugify(name)
        elif entry_type == "model_change" and not session.model:
            model_id = entry.get("modelId", "")
            if isinstance(model_id, str):
                session.model = model_id
        elif entry_type == "message":
            msg = entry.get("message", {})
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "assistant":
                model = msg.get("model", "")
                if isinstance(model, str) and model and not session.model:
                    session.model = model
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "toolCall":
                            continue
                        name = block.get("name", "")
                        if not isinstance(name, str):
                            continue
                        tool_counter[name] += 1
                        args = _tool_args(block)
                        files_set.update(_tool_file_paths(name, args))
            elif role == "toolResult":
                tool_call_id = msg.get("toolCallId", "")
                if isinstance(tool_call_id, str) and tool_call_id:
                    tool_results[tool_call_id] = {
                        "content": _collect_tool_result_text(msg),
                        "is_error": bool(msg.get("isError", False)),
                        "tool_name": msg.get("toolName", ""),
                    }

    session.files_touched = sorted(files_set)
    if tool_counter:
        session.tools_used = ", ".join(
            f"{name}:{count}" for name, count in tool_counter.most_common()
        )

    # Second pass: cleaned transcript/search messages.
    pending_tool_calls: list[dict[str, Any]] = []

    for entry in branch:
        entry_type = entry.get("type")
        ts = _entry_timestamp(entry)

        if entry_type in {"compaction", "branch_summary"}:
            summary = entry.get("summary", "")
            if isinstance(summary, str) and summary.strip():
                label = "Compaction summary" if entry_type == "compaction" else "Branch summary"
                content = f"{label}: {_clean_text(summary)}"
                session.assistant_messages.append(content)
                session.messages.append({"role": "assistant", "content": content, "timestamp": ts})
            continue

        if entry_type != "message":
            continue

        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content", "")
        ts = _message_timestamp(entry, msg)

        if role == "user":
            raw_text = _extract_text_blocks(content, include_image_markers=True)
            cleaned = _clean_text(raw_text)
            if cleaned and not _is_noise_command(cleaned):
                session.user_messages.append(cleaned)
                session.messages.append({"role": "user", "content": cleaned, "timestamp": ts})
            pending_tool_calls = []

        elif role == "assistant":
            parts: list[str] = []
            pending_tool_calls = []

            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        text = _strip_narration(_clean_text(block.get("text", "")))
                        if text:
                            parts.append(text)
                    elif block_type == "toolCall":
                        pending_tool_calls.append(block)
                        name = block.get("name", "")
                        if name == "subagent":
                            parts.append(_subagent_marker(_tool_args(block)))
                    # Skip thinking and image blocks in assistant messages.
            elif isinstance(content, str):
                text = _strip_narration(_clean_text(content))
                if text:
                    parts.append(text)

            if parts:
                combined = "\n\n".join(parts)
                session.assistant_messages.append(combined)
                if session.messages and session.messages[-1]["role"] == "assistant":
                    session.messages[-1]["content"] += "\n\n" + combined
                else:
                    session.messages.append({"role": "assistant", "content": combined, "timestamp": ts})

        elif role == "toolResult":
            # Keep parity with Claude parser: only surface failed bash output.
            tool_call_id = msg.get("toolCallId", "")
            result = tool_results.get(tool_call_id, {}) if isinstance(tool_call_id, str) else {}
            if result.get("is_error") and result.get("tool_name") == "bash":
                result_text = _format_bash_result(str(result.get("content", "")), is_error=True)
                if result_text and session.messages and session.messages[-1]["role"] == "assistant":
                    session.messages[-1]["content"] += f"\n{result_text}"

    session.user_message_count = len(session.user_messages)
    session.assistant_message_count = len(session.assistant_messages)
    return session


def discover_pi_subagents(jsonl_path: str) -> list[SubagentInfo]:
    """Discover pi-subagents nested session files for a parent Pi session."""
    session_stem = os.path.splitext(os.path.basename(jsonl_path))[0]
    root = os.path.join(os.path.dirname(jsonl_path), session_stem)
    if not os.path.isdir(root):
        return []

    pattern = os.path.join(root, "*", "run-*", "session.jsonl")
    results: list[SubagentInfo] = []
    for path in sorted(glob.glob(pattern)):
        rel_parts = os.path.relpath(path, root).split(os.sep)
        run_group = rel_parts[0] if rel_parts else "subagent"
        run_name = rel_parts[1] if len(rel_parts) > 1 else "run"
        agent_id = f"{run_group}-{run_name}"
        results.append(SubagentInfo(
            jsonl_path=path,
            meta_path=None,
            agent_id=agent_id,
            agent_type="subagent",
        ))
    return results


def parse_pi_subagent_jsonl(jsonl_path: str, agent_id: str = "", agent_type: str = "subagent") -> ParsedSubagent:
    """Parse a Pi nested subagent session into ParsedSubagent."""
    result = ParsedSubagent(agent_id=agent_id, agent_type=agent_type or "subagent")

    try:
        parsed = _select_active_branch(_load_jsonl(jsonl_path))
    except OSError:
        return result

    header = parsed.header
    native_id = header.get("id", "")
    if not result.agent_id and isinstance(native_id, str):
        result.agent_id = native_id[:12]

    timestamps = [_entry_timestamp(e) for e in parsed.branch if _entry_timestamp(e)]
    if timestamps:
        result.started_at = timestamps[0]
        result.ended_at = timestamps[-1]
        try:
            t0 = datetime.fromisoformat(result.started_at.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(result.ended_at.replace("Z", "+00:00"))
            result.duration_seconds = max(0, int((t1 - t0).total_seconds()))
        except Exception:
            pass

    tool_counter: Counter[str] = Counter()
    files_set: set[str] = set()
    first_user = True

    for entry in parsed.branch:
        if entry.get("type") != "message":
            continue
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content", "")
        ts = _message_timestamp(entry, msg)

        if role == "user":
            text = _clean_text(_extract_text_blocks(content, include_image_markers=True))
            if text:
                out_role = "prompt" if first_user else "user"
                first_user = False
                result.messages.append({"role": out_role, "content": text, "timestamp": ts})
                if out_role == "prompt":
                    result.initial_prompt = text

        elif role == "assistant":
            parts: list[str] = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = _clean_text(block.get("text", ""))
                        if text:
                            parts.append(text)
                    elif block.get("type") == "toolCall":
                        name = block.get("name", "")
                        args = _tool_args(block)
                        if isinstance(name, str):
                            tool_counter[name] += 1
                            files_set.update(_tool_file_paths(name, args))
                            parts.append(_format_tool_signature(name, args))
            elif isinstance(content, str):
                text = _clean_text(content)
                if text:
                    parts.append(text)
            if parts:
                result.messages.append({"role": "agent", "content": "\n".join(parts), "timestamp": ts})

        elif role == "toolResult" and msg.get("isError"):
            text = _format_bash_result(_collect_tool_result_text(msg), is_error=True)
            if text:
                result.messages.append({"role": "error", "content": text, "timestamp": ts})

    result.files_touched = sorted(files_set)
    result.tool_call_count = sum(tool_counter.values())
    if tool_counter:
        result.tools_used = ", ".join(
            f"{name}:{count}" for name, count in tool_counter.most_common()
        )
    return result
