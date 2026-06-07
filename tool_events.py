"""Provider-neutral tool-call event stream helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from parser import ParsedToolCall


@dataclass(frozen=True)
class ToolUseCandidate:
    tool_name: str
    arguments: dict[str, Any]
    order: int


def iter_tool_use_candidates(call: ParsedToolCall):
    """Yield top-level and nested tool-use candidates for wrapper tools.

    Wrapper tools such as `multi_tool_use.parallel` store nested calls under
    `arguments.tool_uses`; fact builders should share this provider-shape walk
    instead of each re-parsing the wrapper envelope.
    """
    args = call.arguments if isinstance(call.arguments, dict) else {}
    yield ToolUseCandidate(call.tool_name, args, 0)

    tool_uses = args.get("tool_uses")
    if not isinstance(tool_uses, list):
        return

    for order, nested in enumerate(tool_uses, 1):
        if not isinstance(nested, dict):
            continue
        tool_name = nested.get("recipient_name")
        if not isinstance(tool_name, str):
            continue
        parameters = nested.get("parameters")
        yield ToolUseCandidate(tool_name, parameters if isinstance(parameters, dict) else {}, order)



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
