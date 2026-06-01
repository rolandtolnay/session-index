"""SQLite + FTS5 database for session indexing.

Schema: provider-aware sessions table + FTS5 virtual table.
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
    source TEXT,
    native_session_id TEXT,
    source_path TEXT,
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
    transcript_path TEXT,
    tool_log_path TEXT,
    subagent_transcripts TEXT,
    parent_session_path TEXT,
    parent_native_session_id TEXT
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

-- ── Structured fact tables ────────────────────────────────────────────────
-- Owned by sessions and rebuilt with delete-then-insert (idempotent).
-- Coverage tracks the tool-log indexing stage; older rows are populated by
-- `cli.py backfill --no-summary --force`.

-- One row per tool call (main + subagent scope). `tool` is lexically normalized
-- (namespace-stripped, lowercased); raw provider names remain in `tool_name`.
CREATE TABLE IF NOT EXISTS tool_calls (
    session_id TEXT NOT NULL,
    source TEXT,
    scope TEXT,
    sequence INTEGER,
    timestamp TEXT,
    tool_name TEXT,
    tool TEXT,
    is_error INTEGER,
    skill_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool);
CREATE INDEX IF NOT EXISTS idx_tool_calls_skill ON tool_calls(skill_name);

-- One row per successful write/edit file mutation event. Paths are stored exactly
-- as supplied to the tool call; non-mutating tools and failed mutations are excluded.
CREATE TABLE IF NOT EXISTS file_mutations (
    session_id TEXT NOT NULL,
    source TEXT,
    scope TEXT,
    sequence INTEGER,
    timestamp TEXT,
    tool_name TEXT,
    tool TEXT,
    path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_mutations_session ON file_mutations(session_id);
CREATE INDEX IF NOT EXISTS idx_file_mutations_path ON file_mutations(path);

-- One row per normalized subagent run (persisted ParsedSubagentRun facts).
CREATE TABLE IF NOT EXISTS subagent_runs (
    parent_session_id TEXT NOT NULL,
    source TEXT,
    requested_agent_type TEXT,
    observed_agent_type TEXT,
    call_tool TEXT,
    call_sequence INTEGER,
    call_tool_id TEXT,
    child_index INTEGER,
    agent_id TEXT,
    status TEXT,
    started_at TEXT,
    ended_at TEXT,
    duration_seconds INTEGER,
    tool_call_count INTEGER,
    transcript_path TEXT,
    task_preview TEXT,
    match_confidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_subagent_runs_parent ON subagent_runs(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_subagent_runs_type ON subagent_runs(requested_agent_type);

-- One row per asked question. selected_label/was_recommended are NULL when the
-- question was not answered (cancelled). multi_select=1 rows store joined labels
-- with was_recommended=NULL (ambiguous by design).
CREATE TABLE IF NOT EXISTS question_answers (
    session_id TEXT NOT NULL,
    source TEXT,
    sequence INTEGER,
    question_index INTEGER,
    header TEXT,
    question TEXT,
    selected_label TEXT,
    was_recommended INTEGER,
    is_other INTEGER,
    option_count INTEGER,
    multi_select INTEGER
);
CREATE INDEX IF NOT EXISTS idx_question_answers_session ON question_answers(session_id);
CREATE INDEX IF NOT EXISTS idx_question_answers_recommended ON question_answers(was_recommended);
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

    # Migrations — add columns that don't exist in older schemas.
    migrations = [
        ("source", "ALTER TABLE sessions ADD COLUMN source TEXT"),
        ("native_session_id", "ALTER TABLE sessions ADD COLUMN native_session_id TEXT"),
        ("source_path", "ALTER TABLE sessions ADD COLUMN source_path TEXT"),
        ("subagent_transcripts", "ALTER TABLE sessions ADD COLUMN subagent_transcripts TEXT"),
        ("tool_log_path", "ALTER TABLE sessions ADD COLUMN tool_log_path TEXT"),
        ("parent_session_path", "ALTER TABLE sessions ADD COLUMN parent_session_path TEXT"),
        ("parent_native_session_id", "ALTER TABLE sessions ADD COLUMN parent_native_session_id TEXT"),
    ]
    for _column, ddl in migrations:
        try:
            conn.execute(ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # already exists

    # Backfill provider metadata for pre-Pi rows.
    conn.execute("UPDATE sessions SET source = 'claude' WHERE source IS NULL OR source = ''")
    conn.execute("""
        UPDATE sessions
        SET native_session_id = session_id
        WHERE native_session_id IS NULL OR native_session_id = ''
    """)
    conn.commit()

    if close:
        conn.close()


def upsert_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    source: str | None = None,
    native_session_id: str | None = None,
    source_path: str | None = None,
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
    tool_log_path: str | None = None,
    subagent_transcripts: str | None = None,
    parent_session_path: str | None = None,
    parent_native_session_id: str | None = None,
    overwrite_fields: set[str] | None = None,
    commit: bool = True,
) -> None:
    """Insert or update a session, preserving existing values with COALESCE.

    overwrite_fields optionally names columns that should be set to the provided
    value even when that value is NULL. Existing callers omit it and retain the
    historical preserve-on-NULL behavior.
    """
    source = source or "claude"
    native_session_id = native_session_id or session_id
    params = {
        "session_id": session_id,
        "source": source,
        "native_session_id": native_session_id,
        "source_path": source_path,
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
        "tool_log_path": tool_log_path,
        "subagent_transcripts": subagent_transcripts,
        "parent_session_path": parent_session_path,
        "parent_native_session_id": parent_native_session_id,
    }

    conn.execute("""
        INSERT INTO sessions (
            session_id, source, native_session_id, source_path,
            slug, project_path, project, branch, model,
            started_at, ended_at, duration_seconds, user_message_count,
            user_messages, files_touched, tools_used, summary, transcript_path,
            tool_log_path, subagent_transcripts, parent_session_path, parent_native_session_id
        ) VALUES (
            :session_id, :source, :native_session_id, :source_path,
            :slug, :project_path, :project, :branch, :model,
            :started_at, :ended_at, :duration_seconds, :user_message_count,
            :user_messages, :files_touched, :tools_used, :summary, :transcript_path,
            :tool_log_path, :subagent_transcripts, :parent_session_path, :parent_native_session_id
        )
        ON CONFLICT(session_id) DO UPDATE SET
            source = COALESCE(:source, source),
            native_session_id = COALESCE(:native_session_id, native_session_id),
            source_path = COALESCE(:source_path, source_path),
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
            transcript_path = COALESCE(:transcript_path, transcript_path),
            tool_log_path = COALESCE(:tool_log_path, tool_log_path),
            subagent_transcripts = COALESCE(:subagent_transcripts, subagent_transcripts),
            parent_session_path = COALESCE(:parent_session_path, parent_session_path),
            parent_native_session_id = COALESCE(:parent_native_session_id, parent_native_session_id)
    """, params)

    if overwrite_fields:
        allowed = set(params) - {"session_id"}
        invalid = set(overwrite_fields) - allowed
        if invalid:
            raise ValueError(f"Unknown session field(s) for overwrite: {', '.join(sorted(invalid))}")
        assignments = ", ".join(f"{field} = :{field}" for field in sorted(overwrite_fields))
        conn.execute(
            f"UPDATE sessions SET {assignments} WHERE session_id = :session_id",
            params,
        )

    if commit:
        conn.commit()


_FTS5_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def build_fts_query(query: str, use_or: bool = False) -> str:
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


def _build_fts_query(query: str, use_or: bool = False) -> str:
    """Compatibility wrapper for existing tests/imports."""
    return build_fts_query(query, use_or=use_or)


def find_session_candidates(
    conn: sqlite3.Connection,
    query: str | None = None,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    use_or: bool = False,
    session: str | None = None,
) -> list[dict[str, Any]]:
    """Find FTS-backed session candidates with optional structured filters.

    - query provided: FTS5 candidate lookup with optional structured filters, ordered by rank
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
    if session:
        clauses.append("s.session_id = :session")
        params["session"] = session

    if query and query.strip():
        params["query"] = build_fts_query(query, use_or=use_or)

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


def get_session(
    conn: sqlite3.Connection,
    identifier: str,
) -> dict[str, Any] | None:
    """Look up a session by full session_id or unambiguous session_id prefix.

    Resolution order:
      1. Exact session_id match
      2. session_id prefix match (8+ chars, must be unambiguous)
    Returns None if not found or if prefix is ambiguous.
    """
    row = conn.execute(
        """
        SELECT * FROM sessions
        WHERE session_id = :id OR native_session_id = :id
        """,
        {"id": identifier},
    ).fetchone()
    if row:
        return dict(row)

    if len(identifier) >= 8:
        rows = conn.execute(
            """
            SELECT * FROM sessions
            WHERE session_id LIKE :prefix OR native_session_id LIKE :prefix
            """,
            {"prefix": f"{identifier}%"},
        ).fetchall()
        # Deduplicate defensively in case session_id and native_session_id both match.
        by_sid = {row["session_id"]: row for row in rows}
        if len(by_sid) == 1:
            return dict(next(iter(by_sid.values())))

    return None


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


# ── Structured fact persistence ────────────────────────────────────────────

def _replace_rows(
    conn: sqlite3.Connection,
    table: str,
    key_column: str,
    session_id: str,
    rows: list[dict[str, Any]],
    *,
    commit: bool = True,
) -> None:
    """Delete-then-insert all rows for one session. Idempotent re-index."""
    conn.execute(f"DELETE FROM {table} WHERE {key_column} = ?", (session_id,))
    if rows:
        columns = list(rows[0].keys())
        placeholders = ", ".join(f":{c}" for c in columns)
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            rows,
        )
    if commit:
        conn.commit()


def replace_tool_calls(
    conn: sqlite3.Connection, session_id: str, rows: list[dict[str, Any]], *, commit: bool = True,
) -> None:
    _replace_rows(conn, "tool_calls", "session_id", session_id, rows, commit=commit)


def replace_subagent_runs(
    conn: sqlite3.Connection, session_id: str, rows: list[dict[str, Any]], *, commit: bool = True,
) -> None:
    _replace_rows(conn, "subagent_runs", "parent_session_id", session_id, rows, commit=commit)


def replace_question_answers(
    conn: sqlite3.Connection, session_id: str, rows: list[dict[str, Any]], *, commit: bool = True,
) -> None:
    _replace_rows(conn, "question_answers", "session_id", session_id, rows, commit=commit)


def replace_file_mutations(
    conn: sqlite3.Connection, session_id: str, rows: list[dict[str, Any]], *, commit: bool = True,
) -> None:
    _replace_rows(conn, "file_mutations", "session_id", session_id, rows, commit=commit)


def delete_sessions(conn: sqlite3.Connection, session_ids: list[str], *, commit: bool = True) -> int:
    """Delete sessions and all owned fact rows for those session ids."""
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    fact_owners = (
        ("tool_calls", "session_id"),
        ("question_answers", "session_id"),
        ("file_mutations", "session_id"),
        ("subagent_runs", "parent_session_id"),
    )
    for table, key_column in fact_owners:
        conn.execute(f"DELETE FROM {table} WHERE {key_column} IN ({placeholders})", ids)
    cursor = conn.execute(f"DELETE FROM sessions WHERE session_id IN ({placeholders})", ids)
    if commit:
        conn.commit()
    return cursor.rowcount


# ── Read-only SQL escape hatch ─────────────────────────────────────────────

def _get_readonly_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open the database in SQLite read-only mode with query_only enabled."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _run_select(
    conn: sqlite3.Connection, sql: str, max_rows: int = 50,
) -> tuple[list[str], list[list[Any]], bool]:
    """Validate and execute one SELECT/WITH statement on an already read-only connection."""
    stripped = (sql or "").strip()
    while stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if not stripped:
        raise ValueError("Empty query")
    if ";" in stripped:
        raise ValueError("Only a single statement is allowed (no ';')")
    lowered = stripped.lstrip("( \t\r\n").lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("Only SELECT / WITH queries are allowed")

    cursor = conn.execute(stripped)
    columns = [d[0] for d in cursor.description] if cursor.description else []
    fetched = cursor.fetchmany(max_rows + 1)
    truncated = len(fetched) > max_rows
    rows = [list(r) for r in fetched[:max_rows]]
    return columns, rows, truncated


def run_readonly_select(sql: str, max_rows: int = 50) -> tuple[list[str], list[list[Any]], bool]:
    """Run one guarded read-only SELECT/WITH against the session index.

    This owns the full query boundary: SQLite read-only open, query_only defense,
    statement validation, execution, and close. SQL errors propagate verbatim so
    callers can self-correct.
    """
    conn = _get_readonly_connection()
    try:
        return _run_select(conn, sql, max_rows)
    finally:
        conn.close()
