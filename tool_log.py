"""Per-session tool-call log artifact writer.

Writes detailed tool calls/results to a separate Markdown file so cleaned
conversation transcripts can remain focused on user/assistant text.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any

from parser import ParsedToolCall
from transcript import TRANSCRIPT_DIR

TOOL_RESULT_CHAR_LIMIT = 20_000
READ_ONLY_RESULT_CHAR_LIMIT = 2_000
_HALF_RESULT_LIMIT = TOOL_RESULT_CHAR_LIMIT // 2
_HALF_READ_ONLY_RESULT_LIMIT = READ_ONLY_RESULT_CHAR_LIMIT // 2
_TOOL_HEADING_RE = re.compile(r"^## (\d+) — .+ — .+ — .+$")
_READ_ONLY_TOOLS = {
    "read",
    "grep",
    "glob",
    "ls",
    "find",
    "search",
    "open",
    "screenshot",
    "view_image",
}
_AUDIT_RESULT_TOOLS = {
    "write",
    "edit",
    "apply_patch",
    "update",
    "question",
    "askuserquestion",
}
_SHELL_READ_COMMANDS = {
    "awk",
    "cat",
    "fd",
    "find",
    "grep",
    "head",
    "ls",
    "nl",
    "pwd",
    "rg",
    "sed",
    "tail",
    "tree",
    "wc",
}
_GIT_READ_SUBCOMMANDS = {
    "branch",
    "diff",
    "grep",
    "log",
    "ls-files",
    "rev-parse",
    "show",
    "status",
}
_SHELL_WRITE_MARKERS = {">", ">>", "2>", "2>>", "&>", "| tee "}
_SHELL_MUTATING_COMMANDS = {
    "apply_patch",
    "cp",
    "dd",
    "edit",
    "mv",
    "perl",
    "python",
    "python3",
    "rm",
    "ruby",
    "tee",
    "touch",
    "truncate",
    "write",
}


@dataclass(frozen=True)
class ToolLogSection:
    path: str
    sequence: int
    heading: str
    line_start: int | None
    line_end: int | None
    text: str


def _format_time(timestamp: str) -> str:
    """Extract HH:MM:SS from an ISO-like timestamp."""
    if timestamp and len(timestamp) >= 19:
        return timestamp[11:19]
    return "unknown"


def _stringify_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _truncate_result(text: str) -> str:
    if len(text) <= TOOL_RESULT_CHAR_LIMIT:
        return text
    total = len(text)
    return (
        text[:_HALF_RESULT_LIMIT]
        + f"\n\n[truncated: showing first {_HALF_RESULT_LIMIT} and last {_HALF_RESULT_LIMIT} of {total} characters]\n\n"
        + text[-_HALF_RESULT_LIMIT:]
    )


def _compact_read_only_result(text: str) -> str:
    if len(text) <= READ_ONLY_RESULT_CHAR_LIMIT:
        return text
    total = len(text)
    omitted = total - READ_ONLY_RESULT_CHAR_LIMIT
    return (
        text[:_HALF_READ_ONLY_RESULT_LIMIT]
        + f"\n\n[compact read-only result: showing first {_HALF_READ_ONLY_RESULT_LIMIT} "
        + f"and last {_HALF_READ_ONLY_RESULT_LIMIT} of {total} characters; {omitted} omitted]\n\n"
        + text[-_HALF_READ_ONLY_RESULT_LIMIT:]
    )


def _normalize_tool_name(name: str) -> str:
    return (name or "").rsplit(".", 1)[-1].lower()


def _shell_words(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _looks_like_read_only_shell(command: str) -> bool:
    command = (command or "").strip()
    if not command:
        return False
    lowered = command.lower()
    if any(marker in lowered for marker in _SHELL_WRITE_MARKERS):
        return False

    words = _shell_words(command)
    if not words:
        return False
    if any(os.path.basename(word).lower() in _SHELL_MUTATING_COMMANDS for word in words):
        return False

    first = os.path.basename(words[0]).lower()
    if first == "sed" and "-i" in words:
        return False
    if first == "find" and "-delete" in words:
        return False
    if first == "git":
        return len(words) > 1 and words[1].lower() in _GIT_READ_SUBCOMMANDS
    return first in _SHELL_READ_COMMANDS


def _is_read_only_call(call: ParsedToolCall) -> bool:
    tool = _normalize_tool_name(call.tool_name)
    if tool in _READ_ONLY_TOOLS:
        return True
    if tool in _AUDIT_RESULT_TOOLS:
        return False

    args = call.arguments if isinstance(call.arguments, dict) else {}
    command = args.get("command") or args.get("cmd")
    if tool in {"bash", "exec_command", "shell"} and isinstance(command, str):
        return _looks_like_read_only_shell(command)
    return False


def _format_result(call: ParsedToolCall) -> str:
    text = _stringify_result(call.result)
    if not text:
        return "[empty result]"
    if not call.is_error and _is_read_only_call(call):
        return _compact_read_only_result(text)
    return _truncate_result(text)


def _fence_text(text: str) -> str:
    """Keep nested Markdown fences from ending our generated fence."""
    return text.replace("```", "`\u200b``")


def _format_tool_heading(call: ParsedToolCall) -> str:
    """Return the canonical generated Tool Log section heading."""
    seq = f"{call.sequence:03d}" if call.sequence < 1000 else str(call.sequence)
    return f"## {seq} — {call.scope or 'main'} — {call.tool_name or 'tool'} — {_format_time(call.timestamp)}"


def _parse_tool_heading(line: str) -> int | None:
    """Return the numeric sequence from a generated Tool Log heading."""
    match = _TOOL_HEADING_RE.match(line.rstrip("\n"))
    if not match:
        return None
    return int(match.group(1))


def _tool_headings(lines: list[str]) -> list[tuple[int, int]]:
    """Return (line_index, sequence) headings outside fenced blocks."""
    headings: list[tuple[int, int]] = []
    in_fence = False
    for idx, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        sequence = _parse_tool_heading(line)
        if sequence is not None:
            headings.append((idx, sequence))
    return headings


def extract_tool_log_section(path: str, sequence: int) -> ToolLogSection | None:
    """Extract one tool-call section from a generated Tool Log markdown file.

    Returns None for a missing file or a missing sequence. The returned text starts
    at the matching heading and stops immediately before the next tool heading.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return None

    headings = _tool_headings(lines)
    start_idx: int | None = None
    next_heading_idx: int | None = None
    for pos, (idx, heading_sequence) in enumerate(headings):
        if heading_sequence == sequence:
            start_idx = idx
            if pos + 1 < len(headings):
                next_heading_idx = headings[pos + 1][0]
            break
    if start_idx is None:
        return None

    end_idx = next_heading_idx if next_heading_idx is not None else len(lines)

    heading = lines[start_idx].rstrip("\n")
    return ToolLogSection(
        path=path,
        sequence=sequence,
        heading=heading,
        line_start=start_idx + 1,
        line_end=end_idx,
        text="".join(lines[start_idx:end_idx]).rstrip("\n"),
    )


def write_tool_log(
    session_id: str,
    tool_calls: list[ParsedToolCall],
    *,
    project: str | None = None,
    source: str | None = None,
    started_at: str | None = None,
) -> str | None:
    """Write a Markdown tool log for a session. Returns path or None."""
    if not tool_calls:
        return None

    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    path = os.path.join(TRANSCRIPT_DIR, f"{session_id}.tools.md")

    lines: list[str] = [
        f"# Tool log — {session_id}",
        "",
        f"Project: {project or 'unknown'}",
        f"Source: {source or 'unknown'}",
        f"Started: {started_at or 'unknown'}",
        "",
        "---",
        "",
    ]

    for call in tool_calls:
        lines.extend([
            _format_tool_heading(call),
            "",
            f"Status: {'error' if call.is_error else 'ok'}",
            f"Tool call ID: {call.tool_call_id or 'unknown'}",
            "",
            "Arguments:",
            "```json",
            json.dumps(call.arguments or {}, indent=2, ensure_ascii=False, sort_keys=True),
            "```",
            "",
            "Result:",
            "```text",
            _fence_text(_format_result(call)),
            "```",
            "",
        ])

    with open(path, "w") as f:
        f.write("\n".join(lines))

    return path
