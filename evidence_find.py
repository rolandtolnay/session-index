"""Compact Evidence Find candidate retrieval."""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from db import build_fts_query, find_session_candidates
from evidence_model import (
    candidate,
    file_mutation_match,
    file_mutation_session_match,
    question_answer_match,
    session_filter_match,
    session_summary,
    skill_invocation_match,
    subagent_run_match,
    tool_call_match,
    topic_match,
)
from fuzzy_topic import find_fuzzy_topic_candidates
from inspect_refs import QuestionRef, SessionRef, SubagentRef, ToolRef, format_ref


FUZZY_TOPIC_SCOPE_LIMIT = 1000


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


def _exact_topic_has_sessions(conn: sqlite3.Connection, args: dict[str, Any]) -> bool:
    params: dict[str, Any] = {"topic_query": build_fts_query(args["topic"])}
    clauses = _session_filters(args, params)
    where = "WHERE sessions_fts MATCH :topic_query"
    if clauses:
        where += " AND " + " AND ".join(clauses)
    row = conn.execute(f"""
        SELECT 1
        FROM sessions_fts fts
        JOIN sessions s ON s.rowid = fts.rowid
        {where}
        LIMIT 1
    """, params).fetchone()
    return row is not None


def _empty_scoped_sessions_cte() -> str:
    return """
        WITH scoped_sessions AS (
            SELECT s.*, NULL AS topic_rank, NULL AS topic_match_mode, NULL AS fuzzy_score
            FROM sessions s
            WHERE 0
        )
    """


def _sql_string(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _fuzzy_scoped_sessions_cte(conn: sqlite3.Connection, args: dict[str, Any], params: dict[str, Any]) -> str:
    rows = find_fuzzy_topic_candidates(
        conn,
        query=args["topic"],
        project=args.get("project"),
        since=args.get("since"),
        until=args.get("until"),
        session=args.get("session"),
        limit=max(args["limit"], FUZZY_TOPIC_SCOPE_LIMIT),
    )
    if not rows:
        return _empty_scoped_sessions_cte()

    values = ",\n                ".join(
        f"({_sql_string(row['session_id'])}, {float(row['topic_rank'])}, {float(row['fuzzy_score'])})"
        for row in rows
    )
    return f"""
        WITH fuzzy_scope(session_id, topic_rank, fuzzy_score) AS (
                VALUES {values}
            ),
            scoped_sessions AS (
                SELECT s.*, f.topic_rank, 'fuzzy_fallback' AS topic_match_mode, f.fuzzy_score
                FROM fuzzy_scope f
                JOIN sessions s ON s.session_id = f.session_id
            )
    """


def _scoped_sessions_cte(conn: sqlite3.Connection, args: dict[str, Any], params: dict[str, Any]) -> tuple[str, str | None]:
    """Return a CTE containing the sessions in scope for candidate queries."""
    clauses = _session_filters(args, params)
    if args.get("topic"):
        if not _exact_topic_has_sessions(conn, args):
            return _fuzzy_scoped_sessions_cte(conn, args, params), "fuzzy_fallback"

        params["topic_query"] = build_fts_query(args["topic"])
        where = "WHERE sessions_fts MATCH :topic_query"
        if clauses:
            where += " AND " + " AND ".join(clauses)
        return f"""
            WITH scoped_sessions AS (
                SELECT s.*, rank AS topic_rank, 'exact' AS topic_match_mode, NULL AS fuzzy_score
                FROM sessions_fts fts
                JOIN sessions s ON s.rowid = fts.rowid
                {where}
            )
        """, "exact"

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return f"""
        WITH scoped_sessions AS (
            SELECT s.*, NULL AS topic_rank, NULL AS topic_match_mode, NULL AS fuzzy_score
            FROM sessions s
            {where}
        )
    """, None


def _event_order(args: dict[str, Any], topic_scope_mode: str | None, event_order: str) -> str:
    if args.get("topic"):
        if topic_scope_mode == "fuzzy_fallback":
            return f"s.topic_rank DESC, s.started_at DESC, {event_order}"
        return f"s.topic_rank ASC, s.started_at DESC, {event_order}"
    return f"s.started_at DESC, {event_order}"


def _where(clauses: list[str]) -> str:
    return "WHERE " + " AND ".join(clauses) if clauses else ""


def _query(conn: sqlite3.Connection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _with_topic_scope(args: dict[str, Any], row: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
    if args.get("topic") and row.get("topic_match_mode") == "fuzzy_fallback":
        match = dict(match)
        match["topic_scope"] = {
            "topic": args["topic"],
            "match_mode": "fuzzy_fallback",
            "score": row.get("fuzzy_score"),
        }
    return match


def _tool_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"], "tool": (args.get("tool") or "").lower()}
    cte, topic_scope_mode = _scoped_sessions_cte(conn, args, params)
    clauses = ["(LOWER(t.tool) = :tool OR LOWER(t.tool_name) = :tool)"]
    rows = _query(conn, f"""
        {cte}
        SELECT s.*, t.sequence, t.timestamp, t.tool_name, t.tool, t.scope, t.is_error, t.skill_name
        FROM tool_calls t JOIN scoped_sessions s ON s.session_id = t.session_id
        {_where(clauses)}
        ORDER BY {_event_order(args, topic_scope_mode, "t.sequence ASC")}
        LIMIT :limit
    """, params)
    out = []
    for row in rows:
        ref = format_ref(ToolRef(session_id=row["session_id"], sequence=row["sequence"]))
        out.append(candidate(ref, session_summary(row), _with_topic_scope(args, row, tool_call_match(row))))
    return out


def _skill_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"], "skill": (args.get("skill") or "").lower()}
    cte, topic_scope_mode = _scoped_sessions_cte(conn, args, params)
    clauses = ["LOWER(t.skill_name) = :skill"]
    if args.get("tool"):
        params["tool"] = args["tool"].lower()
        clauses.append("(LOWER(t.tool) = :tool OR LOWER(t.tool_name) = :tool)")
    rows = _query(conn, f"""
        {cte}
        SELECT s.*, t.sequence, t.timestamp, t.tool_name, t.tool, t.scope, t.skill_name
        FROM tool_calls t JOIN scoped_sessions s ON s.session_id = t.session_id
        {_where(clauses)}
        ORDER BY {_event_order(args, topic_scope_mode, "t.sequence ASC")}
        LIMIT :limit
    """, params)
    out = []
    for row in rows:
        ref = format_ref(ToolRef(session_id=row["session_id"], sequence=row["sequence"]))
        out.append(candidate(ref, session_summary(row), _with_topic_scope(args, row, skill_invocation_match(row))))
    return out


def _mutation_event_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"], "path": f"%{args.get('mutated')}%"}
    cte, topic_scope_mode = _scoped_sessions_cte(conn, args, params)
    clauses = _mutation_clauses(args, params)
    rows = _query(conn, f"""
        {cte}
        SELECT s.*, m.sequence, m.timestamp, m.tool_name, m.tool, m.scope, m.path
        FROM file_mutations m JOIN scoped_sessions s ON s.session_id = m.session_id
        {_where(clauses)}
        ORDER BY {_event_order(args, topic_scope_mode, "m.sequence ASC, m.path ASC")}
        LIMIT :limit
    """, params)
    out = []
    for row in rows:
        ref = format_ref(ToolRef(session_id=row["session_id"], sequence=row["sequence"]))
        out.append(candidate(ref, session_summary(row), _with_topic_scope(args, row, file_mutation_match(row))))
    return out


def _mutation_clauses(args: dict[str, Any], params: dict[str, Any], alias: str = "m") -> list[str]:
    clauses = [f"{alias}.path LIKE :path"]
    if args.get("tool"):
        params["tool"] = args["tool"].lower()
        clauses.append(f"(LOWER({alias}.tool) = :tool OR LOWER({alias}.tool_name) = :tool)")
    return clauses


def _mutation_session_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"], "path": f"%{args.get('mutated')}%"}
    cte, _topic_scope_mode = _scoped_sessions_cte(conn, args, params)
    clauses = _mutation_clauses(args, params)
    rows = _query(conn, f"""
        {cte},
        selected_sessions AS (
            SELECT s.*
            FROM scoped_sessions s
            WHERE EXISTS (
                SELECT 1 FROM file_mutations m
                WHERE m.session_id = s.session_id AND {' AND '.join(clauses)}
            )
            ORDER BY s.started_at DESC, s.session_id ASC
            LIMIT :limit
        )
        SELECT ss.*, m.sequence AS mutation_sequence, m.path AS mutation_path
        FROM selected_sessions ss
        JOIN file_mutations m ON m.session_id = ss.session_id
        WHERE {' AND '.join(clauses)}
        ORDER BY ss.started_at DESC, ss.session_id ASC, m.sequence ASC, m.path ASC
    """, params)
    if not rows:
        return []

    session_rows: dict[str, dict[str, Any]] = {}
    rows_by_session: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sid = row["session_id"]
        session_rows.setdefault(sid, row)
        rows_by_session.setdefault(sid, []).append(row)

    out = []
    for sid, session_row in session_rows.items():
        rows = rows_by_session[sid]
        path_counts = Counter(row["mutation_path"] for row in rows)
        sequence_counts = Counter(row["mutation_sequence"] for row in rows)
        first_sequence: dict[str, int] = {}
        for row in rows:
            path = row["mutation_path"]
            sequence = row["mutation_sequence"]
            first_sequence[path] = min(first_sequence.get(path, sequence), sequence)
        representative_paths = [
            path for path, _count in sorted(
                path_counts.items(),
                key=lambda item: (-item[1], first_sequence[item[0]], item[0]),
            )[:5]
        ]
        related_tools = [
            format_ref(ToolRef(session_id=sid, sequence=sequence))
            for sequence, _count in sorted(sequence_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ]
        ref = format_ref(SessionRef(session_id=sid))
        match = file_mutation_session_match(
            match_count=len(rows),
            distinct_path_count=len(path_counts),
            representative_paths=representative_paths,
        )
        out.append(candidate(
            ref,
            session_summary(session_row),
            _with_topic_scope(args, session_row, match),
            inspect_refs={"related_tools": related_tools},
        ))
    return out


def _question_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"]}
    cte, topic_scope_mode = _scoped_sessions_cte(conn, args, params)
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
        ORDER BY {_event_order(args, topic_scope_mode, "q.sequence ASC, q.question_index ASC")}
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
        out.append(candidate(ref, session_summary(row), _with_topic_scope(args, row, question_answer_match(row)), inspect_refs={"tool": tool_ref}))
    return out


def _subagent_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": args["limit"], "agent": (args.get("subagent") or "").lower()}
    cte, topic_scope_mode = _scoped_sessions_cte(conn, args, params)
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
        ORDER BY {_event_order(args, topic_scope_mode, "r.child_index ASC")}
        LIMIT :limit
    """, params)
    out = []
    for row in rows:
        ref = format_ref(SubagentRef(session_id=row["session_id"], child_index=row["child_index"]))
        refs: dict[str, str] = {}
        if row["call_sequence"] is not None:
            refs["parent_call"] = format_ref(ToolRef(session_id=row["session_id"], sequence=row["call_sequence"]))
        match_row = {**row, "transcript_path": row["run_transcript_path"]}
        out.append(candidate(ref, session_summary(row), _with_topic_scope(args, row, subagent_run_match(match_row)), inspect_refs=refs))
    return out


def _topic_candidates(conn: sqlite3.Connection, args: dict[str, Any]) -> list[dict[str, Any]]:
    if args.get("topic") and not _exact_topic_has_sessions(conn, args):
        rows = find_fuzzy_topic_candidates(
            conn,
            query=args["topic"],
            project=args.get("project"),
            since=args.get("since"),
            until=args.get("until"),
            limit=args["limit"],
            session=args.get("session"),
        )
        out = []
        for row_dict in rows[:args["limit"]]:
            ref = format_ref(SessionRef(session_id=row_dict["session_id"]))
            match = topic_match(args["topic"], match_mode="fuzzy_fallback", score=row_dict.get("fuzzy_score"))
            out.append(candidate(ref, session_summary(row_dict), match))
        return out

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
    mutation_mode: str = "session",
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
        "mutation_mode": mutation_mode,
    }
    if mutation_mode not in {"session", "event"}:
        raise ValueError("--mutation-mode must be one of: session, event")
    if mutation_mode != "session" and not mutated:
        raise ValueError("--mutation-mode requires --mutated")
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
        if mutation_mode == "event":
            results = _mutation_event_candidates(conn, args)
        else:
            results = _mutation_session_candidates(conn, args)
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
