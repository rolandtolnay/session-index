"""Provider-neutral tool-call event stream helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from parser import ParsedToolCall


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
