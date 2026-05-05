"""Per-session tool-call log artifact writer.

Writes detailed tool calls/results to a separate Markdown file so cleaned
conversation transcripts can remain focused on user/assistant text.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Any

from parser import ParsedToolCall
from transcript import TRANSCRIPT_DIR

TOOL_RESULT_CHAR_LIMIT = 20_000
_HALF_RESULT_LIMIT = TOOL_RESULT_CHAR_LIMIT // 2


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


def _fence_text(text: str) -> str:
    """Keep nested Markdown fences from ending our generated fence."""
    return text.replace("```", "`\u200b``")


def combine_tool_calls(
    session_calls: list[ParsedToolCall],
    subagents: list[Any],
) -> list[ParsedToolCall]:
    """Combine main and subagent calls with stable global sequence numbers."""
    combined: list[ParsedToolCall] = []

    for call in session_calls:
        combined.append(replace(call, scope="main"))

    for sub in subagents:
        agent_id = getattr(sub, "agent_id", "") or "unknown"
        scope = f"agent-{agent_id}"
        for call in getattr(sub, "tool_calls", []):
            combined.append(replace(call, scope=scope))

    return [replace(call, sequence=i) for i, call in enumerate(combined, 1)]


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
        seq = f"{call.sequence:03d}" if call.sequence < 1000 else str(call.sequence)
        lines.extend([
            f"## {seq} — {call.scope or 'main'} — {call.tool_name or 'tool'} — {_format_time(call.timestamp)}",
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
            _fence_text(_truncate_result(_stringify_result(call.result)) or "[empty result]"),
            "```",
            "",
        ])

    with open(path, "w") as f:
        f.write("\n".join(lines))

    return path
