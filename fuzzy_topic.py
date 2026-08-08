"""Deterministic fuzzy topic fallback for Evidence Find."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Any

from rapidfuzz import fuzz, process

from db import top_level_session_predicate

FUZZY_CANDIDATE_POOL_LIMIT = 1000
FUZZY_TOPIC_THRESHOLD = 85.0
FUZZY_ANCHOR_THRESHOLD = 90.0

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _filters(*, project: str | None, since: str | None, until: str | None, session: str | None) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = [top_level_session_predicate("s")]
    params: dict[str, Any] = {"pool_limit": FUZZY_CANDIDATE_POOL_LIMIT}
    if project:
        clauses.append("s.project LIKE :project_pattern")
        params["project_pattern"] = f"{project}%"
    if since:
        clauses.append("s.started_at >= :since")
        params["since"] = since
    if until:
        if len(until) == 10:
            until = f"{until}T23:59:59.999999"
        clauses.append("s.started_at <= :until")
        params["until"] = until
    if session:
        clauses.append("s.session_id = :session")
        params["session"] = session
    return clauses, params


def _recent_sessions(
    conn: sqlite3.Connection,
    *,
    project: str | None,
    since: str | None,
    until: str | None,
    session: str | None,
) -> list[dict[str, Any]]:
    clauses, params = _filters(project=project, since=since, until=until, session=session)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"""
        SELECT s.*
        FROM sessions s
        {where}
        ORDER BY s.started_at DESC, s.session_id ASC
        LIMIT :pool_limit
    """, params).fetchall()
    return [dict(row) for row in rows]


def _grouped_values(conn: sqlite3.Connection, table: str, key_column: str, value_column: str, session_ids: list[str]) -> dict[str, list[str]]:
    if not session_ids:
        return {}
    placeholders = ", ".join("?" for _ in session_ids)
    rows = conn.execute(
        f"SELECT {key_column}, {value_column} FROM {table} WHERE {key_column} IN ({placeholders})",
        session_ids,
    ).fetchall()
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        value = row[value_column]
        if value:
            grouped[row[key_column]].append(str(value))
    return grouped


def _subagent_terms(conn: sqlite3.Connection, session_ids: list[str]) -> dict[str, list[str]]:
    if not session_ids:
        return {}
    placeholders = ", ".join("?" for _ in session_ids)
    rows = conn.execute(f"""
        SELECT parent_session_id, requested_agent_type, observed_agent_type, task_preview
        FROM subagent_runs
        WHERE parent_session_id IN ({placeholders})
    """, session_ids).fetchall()
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        grouped[row["parent_session_id"]].extend(
            str(value) for value in (row["requested_agent_type"], row["observed_agent_type"], row["task_preview"]) if value
        )
    return grouped


def _blob(row: dict[str, Any], mutation_paths: list[str], tool_names: list[str], subagent_terms: list[str]) -> str:
    parts = [
        row.get("summary"),
        row.get("project"),
        row.get("branch"),
        row.get("files_touched"),
        row.get("tools_used"),
        " ".join(mutation_paths),
        " ".join(tool_names),
        " ".join(subagent_terms),
    ]
    return "\n".join(str(part) for part in parts if part)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _score(query: str, blob: str) -> float:
    # Score each query token against its best-matching blob token, then average
    # the strongest ~2/3 of query tokens (always at least two when available).
    # Whole-blob ratios dilute toward zero as session blobs grow, so near-misses
    # like "refersh coordinater" never crossed the threshold. Averaging at least
    # two tokens keeps one shared broad word from carrying an unrelated query,
    # while the 2/3 coverage rule tolerates a missing word in longer queries.
    query_tokens = _tokens(query)
    blob_tokens = list(dict.fromkeys(_tokens(blob)))
    if not query_tokens or not blob_tokens:
        return 0.0
    scores = []
    for token in query_tokens:
        best = process.extractOne(token, blob_tokens, scorer=fuzz.ratio)
        scores.append(best[1] if best else 0.0)
    scores.sort(reverse=True)
    # Anchor rule: at least one query word must appear (near-)exactly. Over
    # thousands of blob tokens every word finds some ~80 lookalike, so without
    # an anchor, all-noise queries would cross the mean threshold.
    if scores[0] < FUZZY_ANCHOR_THRESHOLD:
        return 0.0
    keep = len(scores) if len(scores) <= 2 else max(2, -(-len(scores) * 2 // 3))
    kept = scores[:keep]
    return sum(kept) / len(kept)


def find_fuzzy_topic_candidates(
    conn: sqlite3.Connection,
    *,
    query: str,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    session: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return filtered session rows with fuzzy_score and topic_rank fields."""
    query = (query or "").strip()
    if not query:
        return []

    sessions = _recent_sessions(conn, project=project, since=since, until=until, session=session)
    session_ids = [row["session_id"] for row in sessions]
    mutation_paths = _grouped_values(conn, "file_mutations", "session_id", "path", session_ids)
    tool_names = _grouped_values(conn, "tool_calls", "session_id", "tool", session_ids)
    subagent_terms = _subagent_terms(conn, session_ids)

    scored: list[dict[str, Any]] = []
    for row in sessions:
        sid = row["session_id"]
        score = _score(query, _blob(row, mutation_paths.get(sid, []), tool_names.get(sid, []), subagent_terms.get(sid, [])))
        if score < FUZZY_TOPIC_THRESHOLD:
            continue
        candidate = dict(row)
        candidate["fuzzy_score"] = score
        candidate["match_mode"] = "fuzzy_fallback"
        candidate["topic_rank"] = score
        scored.append(candidate)

    # Stable sorts express the contract directly: score desc, started_at desc, session_id asc.
    scored.sort(key=lambda row: row["session_id"])
    scored.sort(key=lambda row: row.get("started_at") or "", reverse=True)
    scored.sort(key=lambda row: row["fuzzy_score"], reverse=True)
    return scored[:max(1, limit)]
