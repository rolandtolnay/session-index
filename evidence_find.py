"""Compact Evidence Find candidate retrieval."""

from __future__ import annotations

import sqlite3
from typing import Any

from db import build_fts_query, find_session_candidates
from evidence_model import (
    candidate,
    file_mutation_match,
    question_answer_match,
    session_filter_match,
    session_summary,
    skill_invocation_match,
    subagent_run_match,
    tool_call_match,
    topic_match,
)
from inspect_refs import QuestionRef, SessionRef, SubagentRef, ToolRef, format_ref


def _session_filters(args: dict[str, Any], params: dict[str, Any], alias: str = "s") -> list[str]:
    clauses: list[str] = []
    if args.get("project"):
        clauses.append(f"{alias}.project LIKE :project_pattern")
        params["project_pattern"] = f"{args['project']}%"
    if args.get("since"):
        clauses.append(f"{alias}.started_at >= :since")
        params["since"] = args["since"]
    if args.get("until"):
        until = args["until"]
        if len(until) == 10:
            until = f"{until}T23:59:59.999999"
        clauses.append(f"{alias}.started_at <= :until")
        params["until"] = until
    if args.get("session"):
        clauses.append(f"{alias}.session_id = :session")
        params["session"] = args["session"]
    return clauses


def _scoped_sessions_cte(args: dict[str, Any], params: dict[str, Any]) -> str:
    """Return a CTE containing the sessions in scope for candidate queries."""
    clauses = _session_filters(args, params)
    if args.get("topic"):
        params["topic_query"] = build_fts_query(args["topic"])
        where = "WHERE sessions_fts MATCH :topic_query"
        if clauses:
            where += " AND " + " AND ".join(clauses)
        return f"""
            WITH scoped_sessions AS (
                SELECT s.*, rank AS topic_rank
                FROM sessions_fts fts
                JOIN sessions s ON s.rowid = fts.rowid
                {where}
            )
        """

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return f"""
        WITH scoped_sessions AS (
            SELECT s.*, NULL AS topic_rank
            FROM sessions s
            {where}
        )
    """


def _event_order(args: dict[str, Any], event_order: str) -> str:
    if args.get("topic"):
        return f"s.topic_rank ASC, s.started_at DESC, {event_order}"
    return f"s.started_at DESC, {event_order}"


def _where(clauses: list[str]) -> str:
    return "WHERE " + " AND ".join(clauses) if clauses else ""


def _query(conn: sqlite3.Connection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _tool_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"], "tool": (args.get("tool") or "").lower()}
    cte = _scoped_sessions_cte(args, params)
    clauses = ["(LOWER(t.tool) = :tool OR LOWER(t.tool_name) = :tool)"]
    rows = _query(conn, f"""
        {cte}
        SELECT s.*, t.sequence, t.timestamp, t.tool_name, t.tool, t.scope, t.is_error, t.skill_name
        FROM tool_calls t JOIN scoped_sessions s ON s.session_id = t.session_id
        {_where(clauses)}
        ORDER BY {_event_order(args, "t.sequence ASC")}
        LIMIT :limit
    """, params)
    out = []
    for row in rows:
        ref = format_ref(ToolRef(session_id=row["session_id"], sequence=row["sequence"]))
        out.append(candidate(ref, session_summary(row), tool_call_match(row)))
    return out


def _skill_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"], "skill": (args.get("skill") or "").lower()}
    cte = _scoped_sessions_cte(args, params)
    clauses = ["LOWER(t.skill_name) = :skill"]
    if args.get("tool"):
        params["tool"] = args["tool"].lower()
        clauses.append("(LOWER(t.tool) = :tool OR LOWER(t.tool_name) = :tool)")
    rows = _query(conn, f"""
        {cte}
        SELECT s.*, t.sequence, t.timestamp, t.tool_name, t.tool, t.scope, t.skill_name
        FROM tool_calls t JOIN scoped_sessions s ON s.session_id = t.session_id
        {_where(clauses)}
        ORDER BY {_event_order(args, "t.sequence ASC")}
        LIMIT :limit
    """, params)
    out = []
    for row in rows:
        ref = format_ref(ToolRef(session_id=row["session_id"], sequence=row["sequence"]))
        out.append(candidate(ref, session_summary(row), skill_invocation_match(row)))
    return out


def _mutation_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"], "path": f"%{args.get('mutated')}%"}
    cte = _scoped_sessions_cte(args, params)
    clauses = ["m.path LIKE :path"]
    if args.get("tool"):
        params["tool"] = args["tool"].lower()
        clauses.append("(LOWER(m.tool) = :tool OR LOWER(m.tool_name) = :tool)")
    rows = _query(conn, f"""
        {cte}
        SELECT s.*, m.sequence, m.timestamp, m.tool_name, m.tool, m.scope, m.path
        FROM file_mutations m JOIN scoped_sessions s ON s.session_id = m.session_id
        {_where(clauses)}
        ORDER BY {_event_order(args, "m.sequence ASC, m.path ASC")}
        LIMIT :limit
    """, params)
    out = []
    for row in rows:
        ref = format_ref(ToolRef(session_id=row["session_id"], sequence=row["sequence"]))
        out.append(candidate(ref, session_summary(row), file_mutation_match(row)))
    return out


def _question_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"]}
    cte = _scoped_sessions_cte(args, params)
    clauses: list[str] = []
    if args.get("question_recommended") is not None:
        params["recommended"] = 1 if args["question_recommended"] else 0
        clauses.append("q.was_recommended = :recommended")
    rows = _query(conn, f"""
        {cte}
        SELECT s.*, q.sequence, q.question_index, q.header, q.question, q.selected_label,
               q.was_recommended, q.is_other, q.option_count, q.multi_select
        FROM question_answers q JOIN scoped_sessions s ON s.session_id = q.session_id
        {_where(clauses)}
        ORDER BY {_event_order(args, "q.sequence ASC, q.question_index ASC")}
        LIMIT :limit
    """, params)
    out = []
    for row in rows:
        ref = format_ref(QuestionRef(
            session_id=row["session_id"],
            sequence=row["sequence"],
            question_index=row["question_index"],
        ))
        tool_ref = format_ref(ToolRef(session_id=row["session_id"], sequence=row["sequence"]))
        out.append(candidate(ref, session_summary(row), question_answer_match(row), inspect_refs={"tool": tool_ref}))
    return out


def _subagent_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"], "agent": (args.get("subagent") or "").lower()}
    cte = _scoped_sessions_cte(args, params)
    clauses = ["(LOWER(r.requested_agent_type) = :agent OR LOWER(r.observed_agent_type) = :agent)"]
    if args.get("tool"):
        params["tool"] = args["tool"].lower()
        clauses.append("LOWER(r.call_tool) = :tool")
    rows = _query(conn, f"""
        {cte}
        SELECT s.*, r.requested_agent_type, r.observed_agent_type, r.call_tool, r.call_sequence,
               r.child_index, r.agent_id, r.status, r.transcript_path AS run_transcript_path,
               r.task_preview, r.match_confidence, r.tool_call_count
        FROM subagent_runs r JOIN scoped_sessions s ON s.session_id = r.parent_session_id
        {_where(clauses)}
        ORDER BY {_event_order(args, "r.child_index ASC")}
        LIMIT :limit
    """, params)
    out = []
    for row in rows:
        ref = format_ref(SubagentRef(session_id=row["session_id"], child_index=row["child_index"]))
        refs: dict[str, str] = {}
        if row["call_sequence"] is not None:
            refs["parent_call"] = format_ref(ToolRef(session_id=row["session_id"], sequence=row["call_sequence"]))
        match_row = {**row, "transcript_path": row["run_transcript_path"]}
        out.append(candidate(ref, session_summary(row), subagent_run_match(match_row), inspect_refs=refs))
    return out


def _topic_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    rows = find_session_candidates(
        conn,
        query=args.get("topic"),
        project=args.get("project"),
        since=args.get("since"),
        until=args.get("until"),
        limit=args["limit"],
        session=args.get("session"),
    )
    out = []
    match = topic_match(args["topic"]) if args.get("topic") else session_filter_match(
        project=args.get("project"),
        since=args.get("since"),
        until=args.get("until"),
        session=args.get("session"),
    )
    for row in rows[:args["limit"]]:
        row_dict = dict(row)
        ref = format_ref(SessionRef(session_id=row_dict["session_id"]))
        out.append(candidate(ref, session_summary(row_dict), match))
    return out


def find_candidates(
    conn: sqlite3.Connection,
    *,
    topic: str | None = None,
    tool: str | None = None,
    skill: str | None = None,
    mutated: str | None = None,
    subagent: str | None = None,
    question_recommended: bool | None = None,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    session: str | None = None,
    limit: int = 20,
) -> dict[str, list[dict[str, Any]]]:
    """Return compact JSON-ready Evidence Find candidates."""
    args: dict[str, Any] = {
        "topic": topic,
        "tool": tool,
        "skill": skill,
        "mutated": mutated,
        "subagent": subagent,
        "question_recommended": question_recommended,
        "project": project,
        "since": since,
        "until": until,
        "session": session,
        "limit": max(1, limit),
    }
    if question_recommended is not None and (tool or "").lower() != "question":
        raise ValueError("--question-recommended requires --tool question")
    incompatible = [name for name, value in (("--skill", skill), ("--mutated", mutated), ("--subagent", subagent)) if value]
    if len(incompatible) > 1:
        raise ValueError(f"Cannot combine event criteria: {', '.join(incompatible)}")
    if question_recommended is not None and incompatible:
        raise ValueError(f"Cannot combine --question-recommended with {incompatible[0]}")

    if subagent:
        results = _subagent_candidates(conn, args)
    elif mutated:
        results = _mutation_candidates(conn, args)
    elif question_recommended is not None and (tool or "").lower() == "question":
        results = _question_candidates(conn, args)
    elif skill:
        results = _skill_candidates(conn, args)
    elif tool:
        results = _tool_candidates(conn, args)
    elif topic or project or since or until or session:
        results = _topic_candidates(conn, args)
    else:
        raise ValueError("find requires at least one criterion: --topic, --tool, --skill, --mutated, --subagent, --project, --since, --until, or --session")

    return {"results": results[:args["limit"]]}
