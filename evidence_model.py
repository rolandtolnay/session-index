"""Shared JSON builders for Evidence Find/Inspect contracts."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from inspect_refs import SessionRef, format_ref
from tool_log import ToolLogSection
from transcript import TranscriptExcerpt


def session_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Compact session metadata returned by Evidence Find candidates."""
    return {
        "session_id": row["session_id"],
        "project": row["project"],
        "started_at": row["started_at"],
        "summary": row["summary"],
    }


def session_packet(row: dict[str, Any], *, include_summary: bool = False) -> dict[str, Any]:
    """Session metadata returned by Evidence Inspect packets."""
    packet = {
        "session_id": row["session_id"],
        "project": row.get("project"),
        "started_at": row.get("started_at"),
    }
    if include_summary:
        packet["summary"] = row.get("summary")
    return packet


def artifacts(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "transcript_path": row["transcript_path"],
        "tool_log_path": row["tool_log_path"],
        "subagent_transcripts": row["subagent_transcripts"],
    }


def candidate(
    ref: str,
    session: dict[str, Any],
    match: dict[str, Any],
    artifact_paths: dict[str, Any],
    *,
    inspect_refs: dict[str, str] | None = None,
) -> dict[str, Any]:
    refs = {"primary": ref, "context": format_ref(SessionRef(session_id=session["session_id"]))}
    if inspect_refs:
        refs.update(inspect_refs)
    return {
        "ref": ref,
        "inspect_refs": refs,
        "session": session,
        "match": match,
        "artifacts": artifact_paths,
    }


def tool_call_match(row: dict[str, Any], *, file_mutations: list[str] | None = None) -> dict[str, Any]:
    match = {
        "kind": "tool_call",
        "sequence": row["sequence"],
        "tool": row["tool"],
        "tool_name": row["tool_name"],
        "scope": row["scope"],
        "is_error": bool(row["is_error"]),
        "skill_name": row["skill_name"],
    }
    if file_mutations is not None:
        match["file_mutations"] = file_mutations
    return match


def skill_invocation_match(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "skill_invocation",
        "sequence": row["sequence"],
        "tool": row["tool"],
        "tool_name": row["tool_name"],
        "scope": row["scope"],
        "skill_name": row["skill_name"],
    }


def file_mutation_match(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "file_mutation",
        "sequence": row["sequence"],
        "tool": row["tool"],
        "tool_name": row["tool_name"],
        "scope": row["scope"],
        "path": row["path"],
    }


def question_answer_match(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "question_answer",
        "sequence": row["sequence"],
        "question_index": row["question_index"],
        "header": row["header"],
        "question": row["question"],
        "selected_label": row["selected_label"],
        "was_recommended": None if row["was_recommended"] is None else bool(row["was_recommended"]),
        "is_other": bool(row["is_other"]),
        "option_count": row["option_count"],
        "multi_select": bool(row["multi_select"]),
    }


def subagent_run_match(row: dict[str, Any]) -> dict[str, Any]:
    match = {
        "kind": "subagent_run",
        "requested_agent_type": row["requested_agent_type"],
        "observed_agent_type": row["observed_agent_type"],
        "child_index": row["child_index"],
        "agent_id": row["agent_id"],
        "status": row["status"],
        "call_tool": row["call_tool"],
        "call_sequence": row["call_sequence"],
        "task_preview": row["task_preview"],
        "match_confidence": row["match_confidence"],
        "transcript_path": row["transcript_path"],
    }
    if "tool_call_count" in row:
        match["tool_call_count"] = row["tool_call_count"]
    return match


def topic_match(topic: str) -> dict[str, Any]:
    return {"kind": "topic", "topic": topic}


def session_filter_match(*, project: str | None, since: str | None, until: str | None, session: str | None) -> dict[str, Any]:
    return {
        "kind": "session_filter",
        "project": project,
        "since": since,
        "until": until,
        "session": session,
    }


def session_query_match(query: str) -> dict[str, Any]:
    return {"kind": "session", "query": query}


def excerpt_payload(excerpt: TranscriptExcerpt) -> dict[str, Any]:
    return asdict(excerpt)


def tool_log_payload(section: ToolLogSection) -> dict[str, Any]:
    return {
        "artifact": "tool_log",
        "path": section.path,
        "locator": {
            "sequence": section.sequence,
            "heading": section.heading,
            "line_start": section.line_start,
            "line_end": section.line_end,
        },
        "text": section.text,
    }
