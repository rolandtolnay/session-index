"""SQLite + FTS5 database for session indexing.

Schema: 14-column sessions table + FTS5 virtual table.
Uses WAL journal mode for concurrent read/write safety.
FTS sync via INSERT/UPDATE/DELETE triggers.
"""

import os
import sqlite3
from typing import Any

DATA_DIR = os.path.expanduser("~/.session-index")
DB_PATH = os.path.join(DATA_DIR, "sessions.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    slug TEXT,
    project_path TEXT,
    project TEXT,
    branch TEXT,
    model TEXT,
    started_at TEXT,
    ended_at TEXT,
    duration_seconds INTEGER,
    user_message_count INTEGER,
    user_messages TEXT,
    files_touched TEXT,
    tools_used TEXT,
    summary TEXT,
    transcript_path TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    user_messages,
    summary,
    files_touched,
    project,
    content=sessions,
    content_rowid=rowid
);

-- Triggers for FTS sync
CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(rowid, user_messages, summary, files_touched, project)
    VALUES (new.rowid, new.user_messages, new.summary, new.files_touched, new.project);
END;

CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, user_messages, summary, files_touched, project)
    VALUES ('delete', old.rowid, old.user_messages, old.summary, old.files_touched, old.project);
END;

CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, user_messages, summary, files_touched, project)
    VALUES ('delete', old.rowid, old.user_messages, old.summary, old.files_touched, old.project);
    INSERT INTO sessions_fts(rowid, user_messages, summary, files_touched, project)
    VALUES (new.rowid, new.user_messages, new.summary, new.files_touched, new.project);
END;
"""


def get_connection() -> sqlite3.Connection:
    """Get a database connection with WAL mode enabled."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Initialize the database schema."""
    close = conn is None
    if conn is None:
        conn = get_connection()
    conn.executescript(SCHEMA)
    conn.commit()
    if close:
        conn.close()


def upsert_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    slug: str | None = None,
    project_path: str | None = None,
    project: str | None = None,
    branch: str | None = None,
    model: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    duration_seconds: int | None = None,
    user_message_count: int | None = None,
    user_messages: str | None = None,
    files_touched: str | None = None,
    tools_used: str | None = None,
    summary: str | None = None,
    transcript_path: str | None = None,
) -> None:
    """Insert or update a session, preserving existing values with COALESCE."""
    conn.execute("""
        INSERT INTO sessions (
            session_id, slug, project_path, project, branch, model,
            started_at, ended_at, duration_seconds, user_message_count,
            user_messages, files_touched, tools_used, summary, transcript_path
        ) VALUES (
            :session_id, :slug, :project_path, :project, :branch, :model,
            :started_at, :ended_at, :duration_seconds, :user_message_count,
            :user_messages, :files_touched, :tools_used, :summary, :transcript_path
        )
        ON CONFLICT(session_id) DO UPDATE SET
            slug = COALESCE(:slug, slug),
            project_path = COALESCE(:project_path, project_path),
            project = COALESCE(:project, project),
            branch = COALESCE(:branch, branch),
            model = COALESCE(:model, model),
            started_at = COALESCE(:started_at, started_at),
            ended_at = COALESCE(:ended_at, ended_at),
            duration_seconds = COALESCE(:duration_seconds, duration_seconds),
            user_message_count = COALESCE(:user_message_count, user_message_count),
            user_messages = COALESCE(:user_messages, user_messages),
            files_touched = COALESCE(:files_touched, files_touched),
            tools_used = COALESCE(:tools_used, tools_used),
            summary = COALESCE(:summary, summary),
            transcript_path = COALESCE(:transcript_path, transcript_path)
    """, {
        "session_id": session_id,
        "slug": slug,
        "project_path": project_path,
        "project": project,
        "branch": branch,
        "model": model,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "user_message_count": user_message_count,
        "user_messages": user_messages,
        "files_touched": files_touched,
        "tools_used": tools_used,
        "summary": summary,
        "transcript_path": transcript_path,
    })
    conn.commit()


_FTS5_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def _build_fts_query(query: str, use_or: bool = False) -> str:
    """Build an FTS5 query string, quoting terms but preserving operators.

    - Quotes each non-operator term with "term" for special char safety
    - Preserves FTS5 operators (AND, OR, NOT, NEAR) unquoted
    - When use_or=True and no explicit operators present, joins terms with OR
    """
    tokens = query.split()
    has_operators = any(t in _FTS5_OPERATORS for t in tokens)

    parts = []
    for token in tokens:
        if token in _FTS5_OPERATORS:
            parts.append(token)
        else:
            parts.append(f'"{token}"')

    if use_or and not has_operators:
        return " OR ".join(parts)
    return " ".join(parts)


def search_flexible(
    conn: sqlite3.Connection,
    query: str | None = None,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    use_or: bool = False,
) -> list[dict[str, Any]]:
    """Flexible search: FTS5 text + optional project prefix, date range filters.

    - query provided: FTS5 search with optional structured filters, ordered by rank
    - query empty: structured filters only, ordered by started_at DESC
    - nothing provided: returns most recent sessions
    - use_or: join terms with OR instead of implicit AND (ignored if query has explicit operators)
    """
    params: dict[str, Any] = {"limit": limit}
    clauses: list[str] = []

    if project:
        clauses.append("s.project LIKE :project_pattern")
        params["project_pattern"] = f"{project}%"
    if since:
        clauses.append("s.started_at >= :since")
        params["since"] = since
    if until:
        # Bare date (YYYY-MM-DD) should include the full day
        if len(until) == 10:
            until = f"{until}T23:59:59.999999"
        clauses.append("s.started_at <= :until")
        params["until"] = until

    if query and query.strip():
        params["query"] = _build_fts_query(query, use_or=use_or)

        where = "WHERE sessions_fts MATCH :query"
        if clauses:
            where += " AND " + " AND ".join(clauses)

        cursor = conn.execute(f"""
            SELECT s.*, rank
            FROM sessions_fts fts
            JOIN sessions s ON s.rowid = fts.rowid
            {where}
            ORDER BY rank
            LIMIT :limit
        """, params)
    else:
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = conn.execute(f"""
            SELECT s.* FROM sessions s
            {where}
            ORDER BY s.started_at DESC
            LIMIT :limit
        """, params)

    return [dict(row) for row in cursor.fetchall()]


def get_recent_by_project(
    conn: sqlite3.Connection, project: str, limit: int = 5,
) -> list[dict[str, Any]]:
    """Get recent sessions for a specific project."""
    cursor = conn.execute("""
        SELECT * FROM sessions
        WHERE project = :project
        ORDER BY started_at DESC
        LIMIT :limit
    """, {"project": project, "limit": limit})
    return [dict(row) for row in cursor.fetchall()]


def get_recent_cross_project(
    conn: sqlite3.Connection, since: str, exclude_project: str = "", limit: int = 10,
) -> list[dict[str, Any]]:
    """Get recent sessions across all projects since a timestamp."""
    cursor = conn.execute("""
        SELECT * FROM sessions
        WHERE started_at >= :since
        AND (:exclude = '' OR project != :exclude)
        ORDER BY started_at DESC
        LIMIT :limit
    """, {"since": since, "exclude": exclude_project, "limit": limit})
    return [dict(row) for row in cursor.fetchall()]


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Get index statistics."""
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    with_summary = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE summary IS NOT NULL"
    ).fetchone()[0]

    projects = conn.execute("""
        SELECT project, COUNT(*) as count
        FROM sessions
        WHERE project IS NOT NULL AND project != ''
        GROUP BY project
        ORDER BY count DESC
    """).fetchall()

    date_range = conn.execute("""
        SELECT MIN(started_at), MAX(started_at) FROM sessions
    """).fetchone()

    return {
        "total_sessions": total,
        "with_summary": with_summary,
        "missing_summary": total - with_summary,
        "projects": [(row[0], row[1]) for row in projects],
        "earliest": date_range[0],
        "latest": date_range[1],
    }


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS index from scratch."""
    conn.execute("INSERT INTO sessions_fts(sessions_fts) VALUES('rebuild')")
    conn.commit()
