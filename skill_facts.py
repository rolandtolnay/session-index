"""Unified Skill Invocation fact extraction.

This module hides provider-specific skill encodings behind one small row-building
interface. Callers should not need to know whether a skill was invoked through a
slash command, a provider Skill tool call, a Pi skill envelope, or an exact
SKILL.md read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from parser import ParsedToolCall, _NOISE_COMMANDS as CLAUDE_NOISE_COMMANDS
from subagent_runs import ParsedSubagentRun
from tool_events import ToolUseCandidate, iter_tool_use_candidates
from tool_facts import normalize_tool_name

_MAX_TEXT = 240
_SKILL_ENVELOPE_RE = re.compile(r"<skill\b[^>]*\bname=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_BRACKET_COMMAND_RE = re.compile(r"(?m)^\s*\[/([^\]\s]+)\]\s*([^\n]*)")
_SLASH_COMMAND_RE = re.compile(r"(?m)^\s*/([^\s]+)\s*([^\n]*)")
_SKILL_MD_RE = re.compile(r"/skills/([^/]+)/SKILL\.md$")
_LIFECYCLE_COMMANDS = CLAUDE_NOISE_COMMANDS | {
    "/clear", "/exit", "/compact", "/resume", "/new", "/fork", "/clone",
    "/tree", "/init", "/login", "/logout", "/status", "/config",
    "/help", "/model", "/settings", "/session", "/copy", "/export",
    "/share", "/reload", "/hotkeys", "/changelog", "/quit",
}


@dataclass(frozen=True)
class ParsedSkillInvocation:
    timestamp: str | None
    skill_name: str
    invocation_preview: str | None = None
    arguments: str | None = None
    transcript_message_index: int | None = None
    tool_sequence: int | None = None
    child_index: int | None = None
    subagent_transcript_path: str | None = None


@dataclass(frozen=True)
class _DiscoveredInvocation:
    timestamp: str | None
    source_order: tuple[int, int, int]
    invocation: ParsedSkillInvocation


def canonical_skill_name(value: str) -> str:
    """Canonical lowercase skill name preserving meaningful separators."""
    name = (value or "").strip().lower()
    while name.startswith("/"):
        name = name[1:]
    if name.startswith("skill:"):
        name = name[len("skill:"):]
    return name.strip()


def _bounded(value: str | None) -> str | None:
    text = " ".join((value or "").split())
    if not text:
        return None
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1].rstrip() + "…"


def _is_lifecycle_command(raw_name: str) -> bool:
    return f"/{raw_name.strip().lower().lstrip('/')}" in _LIFECYCLE_COMMANDS


def _envelope_event(message: dict[str, Any], message_index: int, match: re.Match[str]) -> _DiscoveredInvocation | None:
    name = canonical_skill_name(match.group(1))
    if not name:
        return None
    timestamp = message.get("timestamp") or None
    invocation = ParsedSkillInvocation(
        timestamp=timestamp,
        skill_name=name,
        invocation_preview=f'<skill name="{name}">',
        transcript_message_index=message_index,
    )
    return _DiscoveredInvocation(timestamp, (0, message_index, match.start()), invocation)


def _slash_event(message: dict[str, Any], message_index: int, match: re.Match[str]) -> _DiscoveredInvocation | None:
    raw_name = match.group(1)
    if _is_lifecycle_command(raw_name):
        return None
    name = canonical_skill_name(raw_name)
    if not name:
        return None
    timestamp = message.get("timestamp") or None
    invocation = ParsedSkillInvocation(
        timestamp=timestamp,
        skill_name=name,
        invocation_preview=f"/{raw_name.strip().lower().lstrip('/')}",
        arguments=_bounded(match.group(2)),
        transcript_message_index=message_index,
    )
    return _DiscoveredInvocation(timestamp, (0, message_index, match.start()), invocation)


def _message_invocations(messages: list[dict[str, Any]]) -> list[_DiscoveredInvocation]:
    discovered: list[_DiscoveredInvocation] = []
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        events: list[_DiscoveredInvocation] = []
        for match in _SKILL_ENVELOPE_RE.finditer(content):
            event = _envelope_event(message, index, match)
            if event is not None:
                events.append(event)
        for regex in (_BRACKET_COMMAND_RE, _SLASH_COMMAND_RE):
            for match in regex.finditer(content):
                event = _slash_event(message, index, match)
                if event is not None:
                    events.append(event)
        discovered.extend(sorted(events, key=lambda event: event.source_order))
    return discovered


def _path_arg(arguments: dict[str, Any]) -> str | None:
    for key in ("file_path", "path"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _run_by_scope(subagent_runs: list[ParsedSubagentRun]) -> dict[str, ParsedSubagentRun]:
    by_scope: dict[str, ParsedSubagentRun] = {}
    for run in subagent_runs:
        if run.agent_id:
            by_scope[f"agent-{run.agent_id}"] = run
    return by_scope


def _with_run_locator(invocation: ParsedSkillInvocation, run: ParsedSubagentRun | None) -> ParsedSkillInvocation:
    if run is None:
        return invocation
    return ParsedSkillInvocation(
        timestamp=invocation.timestamp,
        skill_name=invocation.skill_name,
        invocation_preview=invocation.invocation_preview,
        arguments=invocation.arguments,
        transcript_message_index=invocation.transcript_message_index,
        tool_sequence=invocation.tool_sequence,
        child_index=run.child_index,
        subagent_transcript_path=run.transcript_path or None,
    )


def _provider_skill_invocation(candidate: ToolUseCandidate, call: ParsedToolCall, run: ParsedSubagentRun | None) -> ParsedSkillInvocation | None:
    if normalize_tool_name(candidate.tool_name) != "skill":
        return None
    skill = candidate.arguments.get("skill")
    if not isinstance(skill, str) or not skill.strip():
        return None
    invocation = ParsedSkillInvocation(
        timestamp=call.timestamp or None,
        skill_name=canonical_skill_name(skill),
        tool_sequence=call.sequence or None,
    )
    return _with_run_locator(invocation, run)


def _skill_md_invocation(candidate: ToolUseCandidate, call: ParsedToolCall, run: ParsedSubagentRun | None) -> ParsedSkillInvocation | None:
    if normalize_tool_name(candidate.tool_name) != "read":
        return None
    path = _path_arg(candidate.arguments)
    if not path:
        return None
    match = _SKILL_MD_RE.search(path)
    if not match:
        return None
    invocation = ParsedSkillInvocation(
        timestamp=call.timestamp or None,
        skill_name=canonical_skill_name(match.group(1)),
        tool_sequence=call.sequence or None,
    )
    return _with_run_locator(invocation, run)


def _tool_invocations(combined_tool_calls: list[ParsedToolCall], subagent_runs: list[ParsedSubagentRun]) -> list[_DiscoveredInvocation]:
    runs_by_scope = _run_by_scope(subagent_runs)
    discovered: list[_DiscoveredInvocation] = []
    for call_order, call in enumerate(combined_tool_calls):
        run = runs_by_scope.get(call.scope or "")
        for candidate in iter_tool_use_candidates(call):
            invocation = _provider_skill_invocation(candidate, call, run) or _skill_md_invocation(candidate, call, run)
            if invocation is not None:
                tool_sequence = invocation.tool_sequence if invocation.tool_sequence is not None else call_order
                discovered.append(_DiscoveredInvocation(
                    invocation.timestamp,
                    (1, int(tool_sequence), candidate.order),
                    invocation,
                ))
    return discovered


def _row(session_id: str, source: str, sequence: int, invocation: ParsedSkillInvocation) -> dict[str, object]:
    return {
        "session_id": session_id,
        "source": source,
        "sequence": sequence,
        "timestamp": invocation.timestamp,
        "skill_name": invocation.skill_name,
        "invocation_preview": invocation.invocation_preview,
        "arguments": invocation.arguments,
        "transcript_message_index": invocation.transcript_message_index,
        "tool_sequence": invocation.tool_sequence,
        "child_index": invocation.child_index,
        "subagent_transcript_path": invocation.subagent_transcript_path,
    }


def build_skill_invocation_rows(
    session_id: str,
    source: str,
    session_messages: list[dict[str, str]],
    combined_tool_calls: list[ParsedToolCall],
    subagent_runs: list[ParsedSubagentRun],
) -> list[dict[str, object]]:
    """Build canonical Skill Invocation fact rows for one session."""
    discovered = _message_invocations(session_messages)
    discovered.extend(_tool_invocations(combined_tool_calls, subagent_runs))
    discovered.sort(key=lambda event: (event.timestamp is None, event.timestamp or "", event.source_order))
    return [
        _row(session_id, source, sequence, event.invocation)
        for sequence, event in enumerate(discovered, 1)
    ]
