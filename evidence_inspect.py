"""Resolve Inspection References into bounded JSON Evidence Packets."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict
from typing import Any

from db import get_session
from inspect_refs import InspectionRef, InspectionRefError, format_ref, parse_ref
from tool_log import extract_tool_log_section
from transcript import TranscriptExcerpt, extract_excerpt_objects


class EvidenceInspectError(Exception):
    """Clear JSON-friendly inspect failure."""

    def __init__(self, message: str, *, code: str = "inspect_error", ref: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.ref = ref

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.ref:
            payload["error"]["ref"] = self.ref
        return payload


def _session_packet(session: dict[str, Any], include_summary: bool = False) -> dict[str, Any]:
    packet = {
        "session_id": session["session_id"],
        "project": session.get("project"),
        "started_at": session.get("started_at"),
    }
    if include_summary:
        packet["summary"] = session.get("summary")
    return packet


def _excerpt_payload(excerpt: TranscriptExcerpt) -> dict[str, Any]:
    return asdict(excerpt)


def _tool_log_payload(section) -> dict[str, Any]:
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


def _require_session(conn: sqlite3.Connection, session_id: str, raw_ref: str) -> dict[str, Any]:
    session = get_session(conn, session_id)
    if not session:
        raise EvidenceInspectError(f"Session not found for inspection ref: {session_id}", code="session_not_found", ref=raw_ref)
    return session


def _require_tool_log_section(session: dict[str, Any], sequence: int, raw_ref: str):
    path = session.get("tool_log_path")
    if not path:
        raise EvidenceInspectError("Session has no Tool Log path", code="missing_artifact", ref=raw_ref)
    section = extract_tool_log_section(path, sequence)
    if section is None:
        if not os.path.exists(path):
            raise EvidenceInspectError(f"Tool Log artifact is missing: {path}", code="missing_artifact", ref=raw_ref)
        raise EvidenceInspectError(f"Tool Log sequence not found: {sequence}", code="stale_ref", ref=raw_ref)
    return section


def _inspect_session(conn: sqlite3.Connection, raw_ref: str, ref: InspectionRef, q: str | None, max_snippets: int) -> dict[str, Any]:
    session = _require_session(conn, ref.session_id, raw_ref)
    if not q:
        raise EvidenceInspectError("Session inspection requires --q TEXT", code="missing_query", ref=raw_ref)
    path = session.get("transcript_path")
    if not path:
        raise EvidenceInspectError("Session has no Clean Transcript path", code="missing_artifact", ref=raw_ref)
    if not os.path.exists(path):
        raise EvidenceInspectError(f"Clean Transcript artifact is missing: {path}", code="missing_artifact", ref=raw_ref)
    excerpts = extract_excerpt_objects(path, q.split(), max_blocks=max_snippets, max_lines=200)
    return {
        "ref": raw_ref,
        "session": _session_packet(session),
        "match": {"kind": "session", "query": q},
        "evidence": [_excerpt_payload(excerpt) for excerpt in excerpts],
    }


def _tool_row(conn: sqlite3.Connection, session_id: str, sequence: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM tool_calls WHERE session_id = ? AND sequence = ?",
        (session_id, sequence),
    ).fetchone()
    return dict(row) if row else None


def _mutation_paths(conn: sqlite3.Connection, session_id: str, sequence: int) -> list[str]:
    rows = conn.execute(
        "SELECT path FROM file_mutations WHERE session_id = ? AND sequence = ? ORDER BY path",
        (session_id, sequence),
    ).fetchall()
    return [row["path"] for row in rows]


def _inspect_tool(conn: sqlite3.Connection, raw_ref: str, ref: InspectionRef) -> dict[str, Any]:
    assert ref.sequence is not None
    session = _require_session(conn, ref.session_id, raw_ref)
    tool = _tool_row(conn, ref.session_id, ref.sequence)
    if not tool:
        raise EvidenceInspectError(f"Tool call not found: sequence {ref.sequence}", code="stale_ref", ref=raw_ref)
    section = _require_tool_log_section(session, ref.sequence, raw_ref)
    paths = _mutation_paths(conn, ref.session_id, ref.sequence)
    return {
        "ref": raw_ref,
        "session": _session_packet(session),
        "match": {
            "kind": "tool_call",
            "sequence": tool["sequence"],
            "tool": tool["tool"],
            "tool_name": tool["tool_name"],
            "scope": tool["scope"],
            "is_error": bool(tool["is_error"]),
            "skill_name": tool["skill_name"],
            "file_mutations": paths,
        },
        "evidence": [_tool_log_payload(section)],
    }


def _inspect_question(conn: sqlite3.Connection, raw_ref: str, ref: InspectionRef) -> dict[str, Any]:
    assert ref.sequence is not None and ref.question_index is not None
    session = _require_session(conn, ref.session_id, raw_ref)
    row = conn.execute(
        """
        SELECT * FROM question_answers
        WHERE session_id = ? AND sequence = ? AND question_index = ?
        """,
        (ref.session_id, ref.sequence, ref.question_index),
    ).fetchone()
    if not row:
        raise EvidenceInspectError("Question answer not found", code="stale_ref", ref=raw_ref)
    qrow = dict(row)
    section = _require_tool_log_section(session, ref.sequence, raw_ref)
    return {
        "ref": raw_ref,
        "session": _session_packet(session),
        "match": {
            "kind": "question_answer",
            "sequence": qrow["sequence"],
            "question_index": qrow["question_index"],
            "header": qrow["header"],
            "question": qrow["question"],
            "selected_label": qrow["selected_label"],
            "was_recommended": None if qrow["was_recommended"] is None else bool(qrow["was_recommended"]),
            "is_other": bool(qrow["is_other"]),
            "option_count": qrow["option_count"],
            "multi_select": bool(qrow["multi_select"]),
        },
        "evidence": [_tool_log_payload(section)],
    }


def _read_first_lines(path: str, max_lines: int) -> tuple[str, int]:
    with open(path) as f:
        lines = f.readlines()
    selected = lines[:max_lines]
    return "".join(selected).rstrip("\n"), len(selected)


def _inspect_subagent(conn: sqlite3.Connection, raw_ref: str, ref: InspectionRef, q: str | None, max_snippets: int) -> dict[str, Any]:
    assert ref.child_index is not None
    session = _require_session(conn, ref.session_id, raw_ref)
    row = conn.execute(
        "SELECT * FROM subagent_runs WHERE parent_session_id = ? AND child_index = ?",
        (ref.session_id, ref.child_index),
    ).fetchone()
    if not row:
        raise EvidenceInspectError("Subagent run not found", code="stale_ref", ref=raw_ref)
    run = dict(row)
    path = run.get("transcript_path")
    if not path:
        raise EvidenceInspectError("Subagent run has no transcript path", code="missing_artifact", ref=raw_ref)
    if not os.path.exists(path):
        raise EvidenceInspectError(f"Subagent transcript artifact is missing: {path}", code="missing_artifact", ref=raw_ref)

    if q:
        evidence = [_excerpt_payload(excerpt) for excerpt in extract_excerpt_objects(
            path,
            q.split(),
            artifact="subagent_transcript",
            max_blocks=max_snippets,
            max_lines=200,
        )]
    else:
        text, line_end = _read_first_lines(path, 80)
        evidence = [{
            "artifact": "subagent_transcript",
            "path": path,
            "locator": {"type": "task_area", "line_start": 1, "line_end": line_end},
            "text": text,
        }]

    return {
        "ref": raw_ref,
        "session": _session_packet(session),
        "match": {
            "kind": "subagent_run",
            "requested_agent_type": run["requested_agent_type"],
            "observed_agent_type": run["observed_agent_type"],
            "child_index": run["child_index"],
            "agent_id": run["agent_id"],
            "status": run["status"],
            "call_tool": run["call_tool"],
            "call_sequence": run["call_sequence"],
            "task_preview": run["task_preview"],
            "match_confidence": run["match_confidence"],
            "transcript_path": path,
        },
        "evidence": evidence,
    }


def inspect_ref(
    conn: sqlite3.Connection,
    raw_ref: str,
    *,
    q: str | None = None,
    max_snippets: int = 5,
) -> dict[str, Any]:
    """Resolve one Inspection Reference into an Evidence Packet."""
    try:
        ref = parse_ref(raw_ref)
    except InspectionRefError as e:
        raise EvidenceInspectError(str(e), code="invalid_ref", ref=raw_ref) from e

    canonical = format_ref(ref)
    if ref.kind == "session":
        return _inspect_session(conn, canonical, ref, q, max_snippets)
    if ref.kind == "tool":
        return _inspect_tool(conn, canonical, ref)
    if ref.kind == "question":
        return _inspect_question(conn, canonical, ref)
    if ref.kind == "subagent":
        return _inspect_subagent(conn, canonical, ref, q, max_snippets)
    raise EvidenceInspectError(f"Unsupported inspection ref kind: {ref.kind}", code="invalid_ref", ref=raw_ref)
