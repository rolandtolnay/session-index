"""Resolve Inspection References into bounded JSON Evidence Packets."""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from db import get_session
from evidence_model import (
    snippet_payload,
    question_answer_match,
    skill_invocation_match,
    session_packet,
    session_query_match,
    subagent_run_match,
    tool_call_match,
    tool_log_payload,
)
from inspect_refs import InspectionRefError, QuestionRef, SessionRef, SkillRef, SubagentRef, ToolRef, format_ref, parse_ref
from tool_log import extract_tool_log_section
from transcript import extract_evidence_snippets


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


def _path_metadata(path: str | None) -> dict[str, Any]:
    return {"path": path, "exists": bool(path and os.path.exists(path))}


def _subagent_transcript_count(session: dict[str, Any]) -> int:
    value = session.get("subagent_transcripts") or ""
    return len([path for path in value.split(",") if path.strip()])


def _session_artifacts(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "clean_transcript": _path_metadata(session.get("transcript_path")),
        "tool_log": _path_metadata(session.get("tool_log_path")),
        "subagent_transcripts": {"count": _subagent_transcript_count(session)},
    }


def _session_subagent_refs(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT child_index, requested_agent_type, task_preview
        FROM subagent_runs
        WHERE parent_session_id = ? AND child_index IS NOT NULL
        ORDER BY child_index
        """,
        (session_id,),
    ).fetchall()
    return [
        {
            "ref": format_ref(SubagentRef(session_id=session_id, child_index=row["child_index"])),
            "requested_agent_type": row["requested_agent_type"],
            "task_preview": row["task_preview"],
        }
        for row in rows
    ]


def _session_metadata_packet(conn: sqlite3.Connection, raw_ref: str, session: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": raw_ref,
        "session": session_packet(session),
        "match": match,
        "artifacts": _session_artifacts(session),
        "inspect_refs": {"subagents": _session_subagent_refs(conn, session["session_id"])},
    }


def _inspect_session(conn: sqlite3.Connection, raw_ref: str, ref: SessionRef, q: str | None, max_snippets: int) -> dict[str, Any]:
    session = _require_session(conn, ref.session_id, raw_ref)
    packet = _session_metadata_packet(conn, raw_ref, session, session_query_match(q))
    if not q:
        packet["evidence"] = []
        return packet
    path = session.get("transcript_path")
    if not path:
        raise EvidenceInspectError("Session has no Clean Transcript path", code="missing_artifact", ref=raw_ref)
    if not os.path.exists(path):
        raise EvidenceInspectError(f"Clean Transcript artifact is missing: {path}", code="missing_artifact", ref=raw_ref)
    snippets = extract_evidence_snippets(path, q.split(), max_blocks=max_snippets, max_lines=200)
    packet["evidence"] = [snippet_payload(snippet) for snippet in snippets]
    return packet


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


def _inspect_tool(conn: sqlite3.Connection, raw_ref: str, ref: ToolRef) -> dict[str, Any]:
    session = _require_session(conn, ref.session_id, raw_ref)
    tool = _tool_row(conn, ref.session_id, ref.sequence)
    if not tool:
        raise EvidenceInspectError(f"Tool call not found: sequence {ref.sequence}", code="stale_ref", ref=raw_ref)
    section = _require_tool_log_section(session, ref.sequence, raw_ref)
    paths = _mutation_paths(conn, ref.session_id, ref.sequence)
    return {
        "ref": raw_ref,
        "session": session_packet(session),
        "match": tool_call_match(tool, file_mutations=paths),
        "evidence": [tool_log_payload(section)],
    }


def _skill_row(conn: sqlite3.Connection, session_id: str, sequence: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM skill_invocations WHERE session_id = ? AND sequence = ?",
        (session_id, sequence),
    ).fetchone()
    return dict(row) if row else None


def _skill_locator(row: dict[str, Any]) -> dict[str, Any]:
    locator: dict[str, Any] = {}
    for key in ("transcript_message_index", "tool_sequence", "child_index"):
        if row.get(key) is not None:
            locator[key] = row[key]
    if row.get("invocation_preview"):
        locator["invocation_preview"] = row["invocation_preview"]
    return locator


def _inspect_skill(conn: sqlite3.Connection, raw_ref: str, ref: SkillRef) -> dict[str, Any]:
    session = _require_session(conn, ref.session_id, raw_ref)
    row = _skill_row(conn, ref.session_id, ref.sequence)
    if not row:
        raise EvidenceInspectError("Skill Invocation not found", code="stale_ref", ref=raw_ref)

    clean_path = session.get("transcript_path")
    subagent_path = row.get("subagent_transcript_path")
    primary_path = subagent_path or clean_path

    artifacts: dict[str, Any] = {"primary_transcript": _path_metadata(primary_path)}
    if primary_path != clean_path:
        artifacts["clean_transcript"] = _path_metadata(clean_path)

    return {
        "ref": raw_ref,
        "session": session_packet(session),
        "match": skill_invocation_match(row),
        "artifacts": artifacts,
        "locator": _skill_locator(row),
        "evidence": [],
    }


def _inspect_question(conn: sqlite3.Connection, raw_ref: str, ref: QuestionRef) -> dict[str, Any]:
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
        "session": session_packet(session),
        "match": question_answer_match(qrow),
        "evidence": [tool_log_payload(section)],
    }


def _read_first_lines(path: str, max_lines: int) -> tuple[str, int]:
    with open(path) as f:
        lines = f.readlines()
    selected = lines[:max_lines]
    return "".join(selected).rstrip("\n"), len(selected)


def _inspect_subagent(conn: sqlite3.Connection, raw_ref: str, ref: SubagentRef, q: str | None, max_snippets: int) -> dict[str, Any]:
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
        evidence = [snippet_payload(snippet) for snippet in extract_evidence_snippets(
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
        "session": session_packet(session),
        "match": subagent_run_match(run),
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
    if isinstance(ref, SessionRef):
        return _inspect_session(conn, canonical, ref, q, max_snippets)
    if isinstance(ref, ToolRef):
        return _inspect_tool(conn, canonical, ref)
    if isinstance(ref, QuestionRef):
        return _inspect_question(conn, canonical, ref)
    if isinstance(ref, SkillRef):
        return _inspect_skill(conn, canonical, ref)
    if isinstance(ref, SubagentRef):
        return _inspect_subagent(conn, canonical, ref, q, max_snippets)
    raise EvidenceInspectError(f"Unsupported inspection ref type: {type(ref).__name__}", code="invalid_ref", ref=raw_ref)
