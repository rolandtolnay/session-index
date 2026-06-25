"""In-memory Subagent Run fact normalizer.

Builds queryable facts from already-parsed parent tool calls and discovered child
subagent artifacts. This intentionally does not persist anything yet.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from parser import ParsedToolCall
from subagent_parser import ParsedSubagent


@dataclass(frozen=True)
class ParsedSubagentRun:
    parent_session_id: str
    source: str
    requested_agent_type: str
    call_tool: str
    call_sequence: int | None = None
    call_tool_id: str = ""
    child_index: int | None = None
    run_id: str = ""
    agent_id: str = ""
    observed_agent_type: str = ""
    status: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: int = 0
    tool_call_count: int = 0
    source_path: str = ""
    transcript_path: str = ""
    artifact_path: str = ""
    task_preview: str = ""
    match_confidence: str = ""  # exact | ordered | request_only | artifact_only


_MANAGEMENT_TOOLS = {
    "subagents_list",
    "subagent_status",
    "subagent_inspect",
    "subagent_resume",
    "subagent_interrupt",
    "wait_agent",
    "close_agent",
}

_REQUEST_TOOLS = {
    "Agent",
    "subagent",
    "subagent_run",
    "subagent_parallel",
    "subagent_chain",
    "spawn_agent",
}


def _tool_name(name: str) -> str:
    """Normalize namespaced Pi tool names to their concrete tool/function name."""
    return (name or "").rsplit(".", 1)[-1]


def _preview(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _requested_agent(args: dict[str, Any], default: str = "subagent") -> str:
    for key in ("subagent_type", "agent", "agent_type", "type"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _task_preview(args: dict[str, Any]) -> str:
    for key in ("task", "prompt", "description", "message"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _preview(value)
    return ""


def _copy_call_with_sequence(call: ParsedToolCall, sequence: int) -> ParsedToolCall:
    return replace(call, sequence=call.sequence or sequence)


def _request_fact(
    *,
    parent_session_id: str,
    source: str,
    call: ParsedToolCall,
    requested_agent_type: str,
    task_preview: str = "",
) -> ParsedSubagentRun:
    return ParsedSubagentRun(
        parent_session_id=parent_session_id,
        source=source,
        requested_agent_type=requested_agent_type or "subagent",
        call_tool=_tool_name(call.tool_name),
        call_sequence=call.sequence or None,
        call_tool_id=call.tool_call_id or "",
        task_preview=task_preview,
        match_confidence="request_only",
    )


def _expand_call(parent_session_id: str, source: str, call: ParsedToolCall) -> list[ParsedSubagentRun]:
    name = _tool_name(call.tool_name)
    args = call.arguments if isinstance(call.arguments, dict) else {}

    if name in _MANAGEMENT_TOOLS or name not in _REQUEST_TOOLS:
        return []

    if name in {"Agent", "subagent", "subagent_run", "spawn_agent"}:
        default = "Agent" if name == "Agent" else "subagent"
        return [
            _request_fact(
                parent_session_id=parent_session_id,
                source=source,
                call=call,
                requested_agent_type=_requested_agent(args, default),
                task_preview=_task_preview(args),
            )
        ]

    if name == "subagent_parallel":
        tasks = args.get("tasks")
        if not isinstance(tasks, list):
            tasks = args.get("parallel")
        facts: list[ParsedSubagentRun] = []
        if isinstance(tasks, list):
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                repeat = task.get("count", 1)
                if not isinstance(repeat, int) or repeat < 1:
                    repeat = 1
                for _ in range(repeat):
                    facts.append(_request_fact(
                        parent_session_id=parent_session_id,
                        source=source,
                        call=call,
                        requested_agent_type=_requested_agent(task),
                        task_preview=_task_preview(task),
                    ))
        if facts:
            return facts
        return [_request_fact(
            parent_session_id=parent_session_id,
            source=source,
            call=call,
            requested_agent_type="subagent",
            task_preview=_task_preview(args),
        )]

    if name == "subagent_chain":
        facts: list[ParsedSubagentRun] = []
        steps = args.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                if isinstance(step.get("agent"), str):
                    facts.append(_request_fact(
                        parent_session_id=parent_session_id,
                        source=source,
                        call=call,
                        requested_agent_type=_requested_agent(step),
                        task_preview=_task_preview(step),
                    ))
                parallel = step.get("parallel")
                if isinstance(parallel, list):
                    for task in parallel:
                        if not isinstance(task, dict):
                            continue
                        repeat = task.get("count", 1)
                        if not isinstance(repeat, int) or repeat < 1:
                            repeat = 1
                        for _ in range(repeat):
                            facts.append(_request_fact(
                                parent_session_id=parent_session_id,
                                source=source,
                                call=call,
                                requested_agent_type=_requested_agent(task),
                                task_preview=_task_preview(task),
                            ))
        if facts:
            return facts
        return [_request_fact(
            parent_session_id=parent_session_id,
            source=source,
            call=call,
            requested_agent_type="subagent",
            task_preview=_task_preview(args),
        )]

    return []


def _artifact_fact(parent_session_id: str, source: str, sub: ParsedSubagent, child_index: int) -> ParsedSubagentRun:
    observed = sub.agent_type or "subagent"
    return ParsedSubagentRun(
        parent_session_id=parent_session_id,
        source=source,
        requested_agent_type=observed,
        call_tool="",
        child_index=child_index,
        agent_id=sub.agent_id or "",
        observed_agent_type=observed,
        started_at=sub.started_at or "",
        ended_at=sub.ended_at or "",
        duration_seconds=sub.duration_seconds or 0,
        tool_call_count=sub.tool_call_count or len(sub.tool_calls),
        source_path=getattr(sub, "source_path", "") or "",
        transcript_path=getattr(sub, "transcript_path", "") or "",
        artifact_path=getattr(sub, "artifact_path", "") or "",
        task_preview=_preview(sub.initial_prompt or ""),
        match_confidence="artifact_only",
    )


def _with_artifact(fact: ParsedSubagentRun, sub: ParsedSubagent, child_index: int, confidence: str) -> ParsedSubagentRun:
    return replace(
        fact,
        child_index=child_index,
        agent_id=sub.agent_id or "",
        observed_agent_type=sub.agent_type or "subagent",
        started_at=sub.started_at or "",
        ended_at=sub.ended_at or "",
        duration_seconds=sub.duration_seconds or 0,
        tool_call_count=sub.tool_call_count or len(sub.tool_calls),
        source_path=getattr(sub, "source_path", "") or "",
        transcript_path=getattr(sub, "transcript_path", "") or "",
        artifact_path=getattr(sub, "artifact_path", "") or "",
        match_confidence=confidence,
    )


def build_subagent_runs(
    *,
    parent_session_id: str,
    source: str,
    tool_calls: list[ParsedToolCall],
    subagents: list[ParsedSubagent] | None = None,
) -> list[ParsedSubagentRun]:
    """Build normalized Subagent Run facts from parsed parent calls and artifacts."""
    requests: list[ParsedSubagentRun] = []
    for sequence, call in enumerate(tool_calls, 1):
        requests.extend(_expand_call(parent_session_id, source, _copy_call_with_sequence(call, sequence)))

    artifacts = list(subagents or [])
    facts: list[ParsedSubagentRun] = []
    matched_artifacts: set[int] = set()

    # Explicit IDs are not consistently available yet; preserve today's stable
    # transcript-linking behavior by matching remaining requests by artifact order.
    for idx, request in enumerate(requests):
        if idx < len(artifacts):
            facts.append(_with_artifact(request, artifacts[idx], idx, "ordered"))
            matched_artifacts.add(idx)
        else:
            facts.append(request)

    for idx, sub in enumerate(artifacts):
        if idx not in matched_artifacts:
            facts.append(_artifact_fact(parent_session_id, source, sub, idx))

    return facts
