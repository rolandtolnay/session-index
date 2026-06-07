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
from tool_facts import normalize_tool_name

_MAX_TEXT = 240
_SKILL_ENVELOPE_RE = re.compile(r"<skill\b[^>]*\bname=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_BRACKET_COMMAND_RE = re.compile(r"^\[/([^\]\s]+)\]\s*(.*)$")
_SLASH_COMMAND_RE = re.compile(r"^/([^\s]+)\s*(.*)$")
_SKILL_MD_RE = re.compile(r"/skills/([^/]+)/SKILL\.md$")
_LIFECYCLE_COMMANDS = CLAUDE_NOISE_COMMANDS | {
    "/clear", "/exit", "/compact", "/resume", "/new", "/fork", "/clone",
    "/tree", "/init", "/login", "/logout", "/status", "/config",
    "/help", "/model", "/settings", "/session", "/copy", "/export",
    "/share", "/reload", "/hotkeys", "/changelog", "/quit",
}


@dataclass(frozen=True)
class ParsedSkillInvocation:
    sequence: int
    timestamp: str | None
    skill_name: str
    invocation_preview: str | None = None
    arguments: str | None = None
    transcript_message_index: int | None = None
    tool_sequence: int | None = None
    child_index: int | None = None
    subagent_transcript_path: str | None = None


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


def _skill_envelope_invocations(messages: list[dict[str, Any]]) -> list[ParsedSkillInvocation]:
    invocations: list[ParsedSkillInvocation] = []
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        for match in _SKILL_ENVELOPE_RE.finditer(content):
            name = canonical_skill_name(match.group(1))
            if not name:
                continue
            invocations.append(ParsedSkillInvocation(
                sequence=0,
                timestamp=message.get("timestamp") or None,
                skill_name=name,
                invocation_preview=f'<skill name="{name}">',
                transcript_message_index=index,
            ))
    return invocations


def _slash_command_invocations(messages: list[dict[str, Any]]) -> list[ParsedSkillInvocation]:
    invocations: list[ParsedSkillInvocation] = []
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        timestamp = message.get("timestamp") or None
        for raw_line in str(message.get("content") or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = _BRACKET_COMMAND_RE.match(line) or _SLASH_COMMAND_RE.match(line)
            if not match:
                continue
            raw_name = match.group(1)
            if _is_lifecycle_command(raw_name):
                continue
            name = canonical_skill_name(raw_name)
            if not name:
                continue
            invocations.append(ParsedSkillInvocation(
                sequence=0,
                timestamp=timestamp,
                skill_name=name,
                invocation_preview=f"/{raw_name.strip().lower().lstrip('/')}",
                arguments=_bounded(match.group(2)),
                transcript_message_index=index,
            ))
    return invocations


def _provider_skill_invocation(call: ParsedToolCall) -> ParsedSkillInvocation | None:
    if normalize_tool_name(call.tool_name) != "skill":
        return None
    args = call.arguments if isinstance(call.arguments, dict) else {}
    skill = args.get("skill")
    if not isinstance(skill, str) or not skill.strip():
        return None
    return ParsedSkillInvocation(
        sequence=0,
        timestamp=call.timestamp or None,
        skill_name=canonical_skill_name(skill),
        tool_sequence=call.sequence or None,
    )


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


def _skill_md_invocation(call: ParsedToolCall, runs_by_scope: dict[str, ParsedSubagentRun]) -> ParsedSkillInvocation | None:
    if normalize_tool_name(call.tool_name) != "read":
        return None
    args = call.arguments if isinstance(call.arguments, dict) else {}
    path = _path_arg(args)
    if not path:
        return None
    match = _SKILL_MD_RE.search(path)
    if not match:
        return None
    run = runs_by_scope.get(call.scope or "")
    return ParsedSkillInvocation(
        sequence=0,
        timestamp=call.timestamp or None,
        skill_name=canonical_skill_name(match.group(1)),
        tool_sequence=call.sequence or None,
        child_index=run.child_index if run else None,
        subagent_transcript_path=run.transcript_path if run else None,
    )


def _tool_invocations(combined_tool_calls: list[ParsedToolCall], subagent_runs: list[ParsedSubagentRun]) -> list[ParsedSkillInvocation]:
    runs_by_scope = _run_by_scope(subagent_runs)
    invocations: list[ParsedSkillInvocation] = []
    for call in combined_tool_calls:
        invocation = _provider_skill_invocation(call) or _skill_md_invocation(call, runs_by_scope)
        if invocation is not None:
            invocations.append(invocation)
    return invocations


def _row(session_id: str, source: str, invocation: ParsedSkillInvocation) -> dict[str, object]:
    return {
        "session_id": session_id,
        "source": source,
        "sequence": invocation.sequence,
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
    discovered: list[tuple[str, int, ParsedSkillInvocation]] = []
    invocations = _skill_envelope_invocations(session_messages)
    invocations.extend(_slash_command_invocations(session_messages))
    invocations.extend(_tool_invocations(combined_tool_calls, subagent_runs))
    for order, invocation in enumerate(invocations):
        discovered.append((invocation.timestamp or "", order, invocation))

    discovered.sort(key=lambda item: (item[0], item[1]) if item[0] else ("9999", item[1]))
    rows: list[dict[str, object]] = []
    for sequence, (_, _, invocation) in enumerate(discovered, 1):
        rows.append(_row(session_id, source, ParsedSkillInvocation(
            sequence=sequence,
            timestamp=invocation.timestamp,
            skill_name=invocation.skill_name,
            invocation_preview=invocation.invocation_preview,
            arguments=invocation.arguments,
            transcript_message_index=invocation.transcript_message_index,
            tool_sequence=invocation.tool_sequence,
            child_index=invocation.child_index,
            subagent_transcript_path=invocation.subagent_transcript_path,
        )))
    return rows
