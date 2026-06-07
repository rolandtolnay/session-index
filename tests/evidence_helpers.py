"""Shared test helpers for the canonical Evidence Find/Inspect graph."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import db
from parser import ParsedToolCall
from tool_log import write_tool_log

SESSION_ID = "pi:abc"
DEFAULT_SUMMARY = "Worked on session index evidence retrieval."


def make_memory_conn():
    conn = db.sqlite3.connect(":memory:")
    conn.row_factory = db.sqlite3.Row
    db.init_db(conn)
    return conn


def seed_session_with_mutations(
    conn,
    tmp_path: Path,
    *,
    session_id: str,
    project: str = "session-index",
    started_at: str,
    summary: str = DEFAULT_SUMMARY,
    branch: str | None = "main",
    mutations: list[tuple[int, str, str]] | None = None,
) -> None:
    """Seed one Canonical Session ID with repeated File Mutation rows.

    mutations entries are (sequence, tool, path).
    """
    db.upsert_session(
        conn,
        session_id=session_id,
        source="pi",
        project=project,
        branch=branch,
        started_at=started_at,
        summary=summary,
        user_messages=summary,
        transcript_path=str(tmp_path / f"{session_id}.md"),
        tool_log_path=str(tmp_path / f"{session_id}.tools.md"),
    )
    mutations = mutations or []
    sequences = sorted({sequence for sequence, _tool, _path in mutations})
    db.replace_tool_calls(conn, session_id, [
        {
            "session_id": session_id,
            "source": "pi",
            "scope": "main",
            "sequence": sequence,
            "timestamp": f"{started_at}+{sequence}",
            "tool_name": tool,
            "tool": tool,
            "is_error": 0,
            "skill_name": None,
        }
        for sequence in sequences
        for tool in [next(tool for seq, tool, _path in mutations if seq == sequence)]
    ])
    db.replace_file_mutations(conn, session_id, [
        {
            "session_id": session_id,
            "source": "pi",
            "scope": "main",
            "sequence": sequence,
            "timestamp": f"{started_at}+{sequence}",
            "tool_name": tool,
            "tool": tool,
            "path": path,
        }
        for sequence, tool, path in mutations
    ])


def seed_evidence_graph(
    conn,
    tmp_path: Path,
    *,
    write_artifacts: bool = False,
    summary: str = DEFAULT_SUMMARY,
) -> dict[str, Any]:
    """Seed the canonical evidence session used by find/inspect/CLI tests."""
    transcript_path = tmp_path / f"{SESSION_ID}.md"
    subdir = tmp_path / SESSION_ID
    subagent_path = subdir / "agent-child.md"

    if write_artifacts:
        transcript_path.write_text(
            "proj | main | 2026-05-31\n---\n\n"
            "[user] ────────────────────────────────────────\n"
            "Discuss session index evidence\n\n"
            "[assistant] ──────────────────────────────────\n"
            "Evidence inspect retrieves scoped text.\n"
        )
        subdir.mkdir(exist_ok=True)
        subagent_path.write_text(
            "# scout\nParent: pi:abc\n---\n\n"
            "[prompt] 10:00 ──────────────────────────────\n"
            "Inspect evidence flow\n\n"
            "[agent] 10:01 ──────────────────────────────\n"
            "Found scoped evidence details.\n"
        )
        tool_log_path = write_tool_log(SESSION_ID, [
            ParsedToolCall(sequence=12, scope="main", tool_name="edit", arguments={"path": "etc/prd/example.md"}, result="changed"),
            ParsedToolCall(sequence=14, scope="main", tool_name="question", arguments={"questions": []}, result="Which approach? -> A"),
        ])
    else:
        tool_log_path = str(tmp_path / f"{SESSION_ID}.tools.md")

    db.upsert_session(
        conn,
        session_id=SESSION_ID,
        source="pi",
        project="session-index",
        started_at="2026-05-31T10:00:00Z",
        summary=summary,
        user_messages="Find session index evidence workflow",
        transcript_path=str(transcript_path),
        tool_log_path=tool_log_path,
        subagent_transcripts=str(subagent_path),
    )
    db.replace_tool_calls(conn, SESSION_ID, [
        {"session_id": SESSION_ID, "source": "pi", "scope": "main", "sequence": 12, "timestamp": "2026-05-31T10:01:00Z", "tool_name": "edit", "tool": "edit", "is_error": 0, "skill_name": None},
        {"session_id": SESSION_ID, "source": "pi", "scope": "main", "sequence": 13, "timestamp": "2026-05-31T10:02:00Z", "tool_name": "Skill", "tool": "skill", "is_error": 0, "skill_name": "review"},
        {"session_id": SESSION_ID, "source": "pi", "scope": "main", "sequence": 14, "timestamp": "2026-05-31T10:03:00Z", "tool_name": "question", "tool": "question", "is_error": 0, "skill_name": None},
    ])
    db.replace_file_mutations(conn, SESSION_ID, [{
        "session_id": SESSION_ID,
        "source": "pi",
        "scope": "main",
        "sequence": 12,
        "timestamp": "2026-05-31T10:01:00Z",
        "tool_name": "edit",
        "tool": "edit",
        "path": "etc/prd/example.md",
    }])
    db.replace_question_answers(conn, SESSION_ID, [{
        "session_id": SESSION_ID,
        "source": "pi",
        "sequence": 14,
        "question_index": 0,
        "header": "Choice",
        "question": "Which approach?",
        "selected_label": "A (Recommended)",
        "was_recommended": 1,
        "is_other": 0,
        "option_count": 2,
        "multi_select": 0,
    }])
    db.replace_subagent_runs(conn, SESSION_ID, [{
        "parent_session_id": SESSION_ID,
        "source": "pi",
        "requested_agent_type": "scout",
        "observed_agent_type": "scout",
        "call_tool": "subagent_run",
        "call_sequence": 15,
        "call_tool_id": "call-1",
        "child_index": 0,
        "agent_id": "child",
        "status": "ok",
        "started_at": "2026-05-31T10:04:00Z",
        "ended_at": None,
        "duration_seconds": 10,
        "tool_call_count": 2,
        "transcript_path": str(subagent_path),
        "task_preview": "Inspect evidence flow",
        "match_confidence": "high",
    }])

    return {
        "session_id": SESSION_ID,
        "transcript_path": str(transcript_path),
        "tool_log_path": tool_log_path,
        "subagent_path": str(subagent_path),
    }
