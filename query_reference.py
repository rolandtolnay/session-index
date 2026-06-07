"""LLM-facing reference text for read-only Session Index queries."""

from __future__ import annotations

import re

from db import SCHEMA

_REVIEWED_TABLES = {
    "sessions": "one row per indexed conversation. Source Transcript paths are ingestion metadata, not the normal evidence path.",
    "tool_calls": "one row per indexed tool call. The pair (session_id, sequence) constructs a Tool Inspection Reference.",
    "skill_invocations": "one row per named reusable prompt/workflow template invocation. The pair (session_id, sequence) constructs a Skill Invocation Reference.",
    "file_mutations": "one row per successful write/edit path. This is the precise File Mutation table; sessions.files_touched is broad metadata.",
    "subagent_runs": "one row per Subagent Run requested by a parent session. Use requested_agent_type as the canonical agent label.",
    "question_answers": "one row per asked question. was_recommended is NULL for unanswered or multi-select rows.",
}


def _schema_columns(table_name: str) -> list[str]:
    """Return column names for a CREATE TABLE block in ``SCHEMA``."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table_name)} \((.*?)\n\);",
        SCHEMA,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError(f"Table not found in schema: {table_name}")

    columns: list[str] = []
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        column = line.split(None, 1)[0]
        columns.append(column)
    return columns


def _table_reference() -> str:
    sections = []
    for table_name, semantics in _REVIEWED_TABLES.items():
        columns = ", ".join(_schema_columns(table_name))
        sections.append(f"{table_name}: {semantics}\nColumns: {columns}.")
    return "\n\n".join(sections)


def query_reference() -> str:
    """Return a curated LLM-oriented reference for read-only fact-table queries."""
    return f"""Session Index query reference

Use `query` for counts, rankings, aggregates, and custom joins. Use `find` for compact Evidence Find candidates, then pass Inspection References to `inspect` for scoped evidence text.

Tables and semantics

{_table_reference()}

Construct Inspection References

tool/<session_id>/<sequence> for rows from tool_calls or file_mutations.
skill/<session_id>/<sequence> for rows from skill_invocations.
question/<session_id>/<sequence>/<question_index> for rows from question_answers.
subagent/<parent_session_id>/<child_index> for rows from subagent_runs with child_index.
session/<session_id> for session-level inspection and generated artifact metadata.

Copyable examples

Aggregate tool use:
SELECT tool, COUNT(*) AS n FROM tool_calls GROUP BY tool ORDER BY n DESC LIMIT 20;

Recommended answer rate:
SELECT was_recommended, COUNT(*) AS n FROM question_answers WHERE was_recommended IS NOT NULL AND multi_select=0 GROUP BY was_recommended;

Find skill invocations and build inspect refs:
SELECT 'skill/' || k.session_id || '/' || k.sequence AS ref, k.skill_name, s.project, s.started_at FROM skill_invocations k JOIN sessions s ON s.session_id=k.session_id WHERE k.skill_name='review' ORDER BY s.started_at DESC LIMIT 20;

Aggregate Skill Invocation use:
SELECT skill_name, COUNT(*) AS n FROM skill_invocations GROUP BY skill_name ORDER BY n DESC LIMIT 20;

Subagent runs with inspect refs:
SELECT 'subagent/' || parent_session_id || '/' || child_index AS ref, requested_agent_type, task_preview FROM subagent_runs WHERE child_index IS NOT NULL ORDER BY parent_session_id, child_index LIMIT 20;

Exact File Mutations in one session:
SELECT DISTINCT path FROM file_mutations WHERE session_id='SESSION_ID' ORDER BY path;

File Mutation event trail to inspect:
SELECT 'tool/' || session_id || '/' || sequence AS ref, scope, sequence, tool_name, path FROM file_mutations WHERE session_id='SESSION_ID' ORDER BY sequence, path;
"""
