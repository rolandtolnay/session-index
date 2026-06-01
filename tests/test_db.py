"""Tests for the SQLite + FTS5 database."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from db import (
    init_db,
    upsert_session,
    find_session_candidates,
    get_session,
    get_recent_by_project,
    get_stats,
    rebuild_fts,
    _build_fts_query,
    run_readonly_select,
    replace_tool_calls,
    replace_subagent_runs,
    replace_question_answers,
    replace_file_mutations,
    delete_sessions,
)
from query_reference import query_reference


def _make_conn():
    """Create an in-memory database for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_init_db():
    conn = _make_conn()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {row[0] for row in tables}
    assert "sessions" in names
    assert "sessions_fts" in names
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "parent_session_path" in columns
    assert "parent_native_session_id" in columns
    assert "tool_log_path" in columns
    conn.close()


def test_upsert_insert():
    conn = _make_conn()
    upsert_session(conn, session_id="test-1", project="myproject", summary="did stuff")
    row = conn.execute("SELECT * FROM sessions WHERE session_id='test-1'").fetchone()
    assert row is not None
    assert row["project"] == "myproject"
    assert row["summary"] == "did stuff"
    assert row["source"] == "claude"
    assert row["native_session_id"] == "test-1"
    conn.close()


def test_get_session_by_native_pi_prefix():
    conn = _make_conn()
    upsert_session(
        conn,
        session_id="pi:019dde8f-eeb6-76dc-94fc-b173b083e8d2",
        source="pi",
        native_session_id="019dde8f-eeb6-76dc-94fc-b173b083e8d2",
        project="session-index",
        parent_session_path="2026-04-30T18-40-41-826Z_019ddfb1-7362-7526-8b21-8a6d77c82fe0.jsonl",
        parent_native_session_id="019ddfb1-7362-7526-8b21-8a6d77c82fe0",
    )

    stored = conn.execute(
        "SELECT parent_session_path, parent_native_session_id FROM sessions WHERE session_id = ?",
        ("pi:019dde8f-eeb6-76dc-94fc-b173b083e8d2",),
    ).fetchone()
    assert stored["parent_session_path"].endswith("019ddfb1-7362-7526-8b21-8a6d77c82fe0.jsonl")
    assert stored["parent_native_session_id"] == "019ddfb1-7362-7526-8b21-8a6d77c82fe0"

    row = get_session(conn, "019dde8f")
    assert row is not None
    assert row["session_id"] == "pi:019dde8f-eeb6-76dc-94fc-b173b083e8d2"
    assert row["source"] == "pi"
    conn.close()


def test_upsert_preserves_existing():
    conn = _make_conn()
    upsert_session(conn, session_id="test-2", project="proj", summary="first summary", tool_log_path="/tmp/tools.md")
    # Update without summary/tool_log_path — should preserve existing
    upsert_session(conn, session_id="test-2", branch="feature-x")
    row = conn.execute("SELECT * FROM sessions WHERE session_id='test-2'").fetchone()
    assert row["summary"] == "first summary"
    assert row["tool_log_path"] == "/tmp/tools.md"
    assert row["branch"] == "feature-x"
    conn.close()


def test_upsert_overwrites_with_value():
    conn = _make_conn()
    upsert_session(conn, session_id="test-3", summary="old", tool_log_path="/tmp/old.tools.md")
    upsert_session(conn, session_id="test-3", summary="new", tool_log_path="/tmp/new.tools.md")
    row = conn.execute("SELECT * FROM sessions WHERE session_id='test-3'").fetchone()
    assert row["summary"] == "new"
    assert row["tool_log_path"] == "/tmp/new.tools.md"
    conn.close()


def test_find_session_candidates_fts():
    conn = _make_conn()
    upsert_session(
        conn, session_id="s1", project="dashboard",
        user_messages="Fix the token refresh bug",
        summary="Fixed token refresh in auth module",
    )
    upsert_session(
        conn, session_id="s2", project="backend",
        user_messages="Add pagination to API",
        summary="Implemented cursor-based pagination",
    )
    results = find_session_candidates(conn, query="token refresh")
    assert len(results) >= 1
    assert any(r["session_id"] == "s1" for r in results)
    conn.close()


def test_get_recent_by_project():
    conn = _make_conn()
    upsert_session(conn, session_id="r1", project="app", started_at="2026-01-01T00:00:00Z")
    upsert_session(conn, session_id="r2", project="app", started_at="2026-01-02T00:00:00Z")
    upsert_session(conn, session_id="r3", project="other", started_at="2026-01-03T00:00:00Z")

    results = get_recent_by_project(conn, "app")
    assert len(results) == 2
    assert results[0]["session_id"] == "r2"  # most recent first
    conn.close()


def test_get_stats():
    conn = _make_conn()
    upsert_session(conn, session_id="stat1", project="a", summary="yes")
    upsert_session(conn, session_id="stat2", project="a")
    upsert_session(conn, session_id="stat3", project="b", summary="yes")

    stats = get_stats(conn)
    assert stats["total_sessions"] == 3
    assert stats["with_summary"] == 2
    assert stats["missing_summary"] == 1
    assert len(stats["projects"]) == 2
    conn.close()


def test_rebuild_fts():
    conn = _make_conn()
    upsert_session(conn, session_id="rb1", user_messages="test rebuild", summary="rebuilding")
    rebuild_fts(conn)
    results = find_session_candidates(conn, query="rebuild")
    assert len(results) >= 1
    conn.close()


# ── _build_fts_query tests ────────────────────────────────────────────────


def test_build_fts_query_and_default():
    assert _build_fts_query("auth token") == '"auth" "token"'


def test_build_fts_query_or_mode():
    assert _build_fts_query("auth token", use_or=True) == '"auth" OR "token"'


def test_build_fts_query_preserves_or_operator():
    assert _build_fts_query("auth OR token") == '"auth" OR "token"'


def test_build_fts_query_preserves_not_operator():
    assert _build_fts_query("auth NOT token") == '"auth" NOT "token"'


def test_build_fts_query_single_term():
    assert _build_fts_query("auth") == '"auth"'
    assert _build_fts_query("auth", use_or=True) == '"auth"'


def test_build_fts_query_explicit_ops_ignore_use_or():
    # When query already has explicit operators, use_or should not add more
    result = _build_fts_query("auth OR token", use_or=True)
    assert result == '"auth" OR "token"'


# ── find_session_candidates tests ─────────────────────────────────────────


def test_find_session_candidates_or_mode():
    conn = _make_conn()
    upsert_session(conn, session_id="or1", project="proj",
                   user_messages="auth token refresh", summary="Fixed auth tokens")
    upsert_session(conn, session_id="or2", project="proj",
                   user_messages="add pagination", summary="Added pagination")
    # AND: only or1 matches (has both "auth" and "token")
    results_and = find_session_candidates(conn, query="auth pagination", use_or=False)
    assert len(results_and) == 0  # no session has both
    # OR: both match (or1 has "auth", or2 has "pagination")
    results_or = find_session_candidates(conn, query="auth pagination", use_or=True)
    assert len(results_or) == 2
    conn.close()


def test_find_session_candidates_fts_only():
    conn = _make_conn()
    upsert_session(conn, session_id="sf1", project="proj",
                   user_messages="auth token refresh", summary="Fixed auth tokens")
    upsert_session(conn, session_id="sf2", project="proj",
                   user_messages="add pagination", summary="Added pagination")
    results = find_session_candidates(conn, query="auth token")
    assert len(results) >= 1
    assert any(r["session_id"] == "sf1" for r in results)
    assert not any(r["session_id"] == "sf2" for r in results)
    conn.close()


def test_find_session_candidates_project_prefix():
    conn = _make_conn()
    upsert_session(conn, session_id="sp1", project="synapto-backend",
                   started_at="2026-03-15T00:00:00Z")
    upsert_session(conn, session_id="sp2", project="synapto-web",
                   started_at="2026-03-16T00:00:00Z")
    upsert_session(conn, session_id="sp3", project="dashboard-web",
                   started_at="2026-03-17T00:00:00Z")
    results = find_session_candidates(conn, project="synapto")
    assert len(results) == 2
    ids = {r["session_id"] for r in results}
    assert ids == {"sp1", "sp2"}
    conn.close()


def test_find_session_candidates_date_range():
    conn = _make_conn()
    upsert_session(conn, session_id="sd1", project="p",
                   started_at="2026-02-15T10:00:00Z")
    upsert_session(conn, session_id="sd2", project="p",
                   started_at="2026-03-15T10:00:00Z")
    upsert_session(conn, session_id="sd3", project="p",
                   started_at="2026-04-15T10:00:00Z")
    results = find_session_candidates(conn, since="2026-03-01", until="2026-03-31")
    assert len(results) == 1
    assert results[0]["session_id"] == "sd2"
    conn.close()


def test_find_session_candidates_combined():
    conn = _make_conn()
    upsert_session(conn, session_id="sc1", project="dashboard-web",
                   started_at="2026-03-10T00:00:00Z",
                   user_messages="debug auth flow", summary="Debugged auth")
    upsert_session(conn, session_id="sc2", project="dashboard-web",
                   started_at="2026-02-10T00:00:00Z",
                   user_messages="debug auth flow", summary="Debugged auth old")
    upsert_session(conn, session_id="sc3", project="backend",
                   started_at="2026-03-10T00:00:00Z",
                   user_messages="debug auth flow", summary="Backend auth debug")
    # Only sc1 matches: query + project + date
    results = find_session_candidates(conn, query="debug auth",
                              project="dashboard", since="2026-03-01")
    assert len(results) == 1
    assert results[0]["session_id"] == "sc1"
    conn.close()


def test_find_session_candidates_no_filters():
    conn = _make_conn()
    upsert_session(conn, session_id="sn1", project="a",
                   started_at="2026-03-01T00:00:00Z")
    upsert_session(conn, session_id="sn2", project="b",
                   started_at="2026-03-10T00:00:00Z")
    upsert_session(conn, session_id="sn3", project="c",
                   started_at="2026-03-05T00:00:00Z")
    results = find_session_candidates(conn, limit=3)
    assert len(results) == 3
    # Most recent first
    assert results[0]["session_id"] == "sn2"
    assert results[1]["session_id"] == "sn3"
    assert results[2]["session_id"] == "sn1"
    conn.close()


def test_find_session_candidates_until_inclusive():
    conn = _make_conn()
    upsert_session(conn, session_id="su1", project="p",
                   started_at="2026-03-31T23:30:00Z")
    upsert_session(conn, session_id="su2", project="p",
                   started_at="2026-04-01T00:30:00Z")
    # Bare date until should include full day
    results = find_session_candidates(conn, until="2026-03-31")
    assert len(results) == 1
    assert results[0]["session_id"] == "su1"
    conn.close()


# ── get_session tests ────────────────────────────────────────────────────────


def test_get_session_does_not_match_slug():
    """Slug lookup was removed — slugs aren't unique, session_id is the only key."""
    conn = _make_conn()
    upsert_session(conn, session_id="sess-abc-123-full", slug="fixing-login-bug",
                   project="proj", summary="Fixed login")
    assert get_session(conn, "fixing-login-bug") is None
    conn.close()


def test_get_session_by_full_id():
    conn = _make_conn()
    upsert_session(conn, session_id="sess-abc-123-full", project="proj")
    result = get_session(conn, "sess-abc-123-full")
    assert result is not None
    assert result["session_id"] == "sess-abc-123-full"
    conn.close()


def test_get_session_by_prefix():
    conn = _make_conn()
    upsert_session(conn, session_id="sess-abc-123-full-uuid", project="proj")
    result = get_session(conn, "sess-abc-123-full")
    assert result is not None
    assert result["session_id"] == "sess-abc-123-full-uuid"
    conn.close()


def test_get_session_ambiguous_prefix():
    conn = _make_conn()
    upsert_session(conn, session_id="sess-abc-111-aaa", project="proj")
    upsert_session(conn, session_id="sess-abc-222-bbb", project="proj")
    result = get_session(conn, "sess-abc-")
    assert result is None
    conn.close()


def test_get_session_not_found():
    conn = _make_conn()
    result = get_session(conn, "nonexistent")
    assert result is None
    conn.close()


def test_get_session_short_prefix_rejected():
    """Prefix shorter than 8 chars should not match."""
    conn = _make_conn()
    upsert_session(conn, session_id="abcdefghij", project="proj")
    assert get_session(conn, "abcdefg") is None   # 7 chars
    assert get_session(conn, "abcdefgh") is not None  # 8 chars
    conn.close()


# ── Fact tables: schema + persistence ──────────────────────────────────────


def test_init_db_creates_fact_tables():
    conn = _make_conn()
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tool_calls", "subagent_runs", "question_answers", "file_mutations"} <= names
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_file_mutations_session", "idx_file_mutations_path"} <= indexes
    columns = {row[1] for row in conn.execute("PRAGMA table_info(file_mutations)")}
    assert {"session_id", "source", "scope", "sequence", "timestamp", "tool_name", "tool", "path"} <= columns
    conn.close()


def test_replace_tool_calls_idempotent():
    conn = _make_conn()
    rows = [{
        "session_id": "s1", "source": "claude", "scope": "main", "sequence": 1,
        "timestamp": None, "tool_name": "Bash", "tool": "bash", "is_error": 0, "skill_name": None,
    }]
    replace_tool_calls(conn, "s1", rows)
    replace_tool_calls(conn, "s1", rows)  # re-index must not duplicate
    assert conn.execute("SELECT COUNT(*) FROM tool_calls WHERE session_id='s1'").fetchone()[0] == 1
    replace_tool_calls(conn, "s1", [])  # empty clears
    assert conn.execute("SELECT COUNT(*) FROM tool_calls WHERE session_id='s1'").fetchone()[0] == 0
    conn.close()


def test_replace_file_mutations_idempotent_and_empty_clears():
    conn = _make_conn()
    rows = [{
        "session_id": "s1", "source": "claude", "scope": "main", "sequence": 1,
        "timestamp": None, "tool_name": "Edit", "tool": "edit", "path": "src/app.py",
    }]

    replace_file_mutations(conn, "s1", rows)
    replace_file_mutations(conn, "s1", rows)

    stored = conn.execute("SELECT tool_name, tool, path FROM file_mutations WHERE session_id='s1'").fetchall()
    assert [tuple(row) for row in stored] == [("Edit", "edit", "src/app.py")]

    replace_file_mutations(conn, "s1", [])
    assert conn.execute("SELECT COUNT(*) FROM file_mutations WHERE session_id='s1'").fetchone()[0] == 0
    conn.close()


def test_replace_question_answers_and_subagent_runs_roundtrip():
    conn = _make_conn()
    replace_question_answers(conn, "s1", [{
        "session_id": "s1", "source": "claude", "sequence": 1, "question_index": 0,
        "header": "H", "question": "Q", "selected_label": "A", "was_recommended": 1,
        "is_other": 0, "option_count": 2, "multi_select": 0,
    }])
    qa = conn.execute("SELECT selected_label, was_recommended FROM question_answers WHERE session_id='s1'").fetchone()
    assert qa["selected_label"] == "A" and qa["was_recommended"] == 1

    replace_subagent_runs(conn, "s1", [{
        "parent_session_id": "s1", "source": "claude", "requested_agent_type": "Explore",
        "observed_agent_type": None, "call_tool": "Agent", "call_sequence": 1, "call_tool_id": "t",
        "child_index": None, "agent_id": None, "status": None, "started_at": None, "ended_at": None,
        "duration_seconds": None, "tool_call_count": None, "transcript_path": None,
        "task_preview": None, "match_confidence": "request_only",
    }])
    sr = conn.execute("SELECT requested_agent_type FROM subagent_runs WHERE parent_session_id='s1'").fetchone()
    assert sr["requested_agent_type"] == "Explore"
    conn.close()


def test_delete_sessions_removes_owned_fact_rows():
    conn = _make_conn()
    upsert_session(conn, session_id="owned-1", project="p")
    replace_tool_calls(conn, "owned-1", [{
        "session_id": "owned-1", "source": "claude", "scope": "main", "sequence": 1,
        "timestamp": None, "tool_name": "Read", "tool": "read", "is_error": 0,
        "skill_name": None,
    }])
    replace_question_answers(conn, "owned-1", [{
        "session_id": "owned-1", "source": "claude", "sequence": 1, "question_index": 0,
        "header": "H", "question": "Q", "selected_label": "A", "was_recommended": 1,
        "is_other": 0, "option_count": 2, "multi_select": 0,
    }])
    replace_subagent_runs(conn, "owned-1", [{
        "parent_session_id": "owned-1", "source": "claude", "requested_agent_type": "Explore",
        "observed_agent_type": None, "call_tool": "Agent", "call_sequence": 1, "call_tool_id": "t",
        "child_index": None, "agent_id": None, "status": None, "started_at": None, "ended_at": None,
        "duration_seconds": None, "tool_call_count": None, "transcript_path": None,
        "task_preview": None, "match_confidence": "request_only",
    }])
    replace_file_mutations(conn, "owned-1", [{
        "session_id": "owned-1", "source": "claude", "scope": "main", "sequence": 2,
        "timestamp": None, "tool_name": "Edit", "tool": "edit", "path": "src/app.py",
    }])

    assert delete_sessions(conn, ["owned-1"]) == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id='owned-1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tool_calls WHERE session_id='owned-1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM question_answers WHERE session_id='owned-1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM subagent_runs WHERE parent_session_id='owned-1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM file_mutations WHERE session_id='owned-1'").fetchone()[0] == 0
    conn.close()


def test_query_reference_describes_tables_without_raw_ddl():
    reference = query_reference()
    assert "tool_calls" in reference
    assert "subagent_runs" in reference
    assert "question_answers" in reference
    assert "file_mutations" in reference
    assert "sessions" in reference
    assert "tool/<session_id>/<sequence>" in reference
    assert "CREATE TABLE" not in reference


# ── run_readonly_select (read-only escape hatch) ────────────────────────────


def _make_file_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sessions.db"))
    conn = db.get_connection()
    init_db(conn)
    return conn


def test_run_readonly_select_basic(tmp_path, monkeypatch):
    conn = _make_file_db(tmp_path, monkeypatch)
    upsert_session(conn, session_id="q1", project="p", summary="hello")
    conn.close()

    cols, rows, truncated = run_readonly_select("SELECT session_id, project FROM sessions")

    assert cols == ["session_id", "project"]
    assert rows == [["q1", "p"]]
    assert truncated is False


def test_run_readonly_select_truncation_flag(tmp_path, monkeypatch):
    conn = _make_file_db(tmp_path, monkeypatch)
    for i in range(5):
        upsert_session(conn, session_id=f"t{i}", project="p")
    conn.close()

    _cols, rows, truncated = run_readonly_select("SELECT session_id FROM sessions", max_rows=3)

    assert len(rows) == 3
    assert truncated is True


def test_run_readonly_select_allows_with_cte_and_strips_semicolon(tmp_path, monkeypatch):
    conn = _make_file_db(tmp_path, monkeypatch)
    upsert_session(conn, session_id="w1", project="p")
    conn.close()

    _cols, rows, _ = run_readonly_select("WITH x AS (SELECT session_id FROM sessions) SELECT * FROM x;  ")

    assert rows == [["w1"]]


def test_run_readonly_select_rejects_non_select(tmp_path, monkeypatch):
    conn = _make_file_db(tmp_path, monkeypatch)
    conn.close()

    for bad in ["DELETE FROM sessions", "UPDATE sessions SET summary='x'",
                "INSERT INTO sessions(session_id) VALUES('z')", "DROP TABLE sessions", ""]:
        try:
            run_readonly_select(bad)
            assert False, f"expected rejection for {bad!r}"
        except ValueError:
            pass


def test_run_readonly_select_rejects_multi_statement(tmp_path, monkeypatch):
    conn = _make_file_db(tmp_path, monkeypatch)
    conn.close()

    try:
        run_readonly_select("SELECT 1; SELECT 2")
        assert False, "expected multi-statement rejection"
    except ValueError:
        pass


def test_run_readonly_select_blocks_writes_even_if_lexically_select(tmp_path, monkeypatch):
    conn = _make_file_db(tmp_path, monkeypatch)
    upsert_session(conn, session_id="ro1", project="p")
    conn.close()

    try:
        run_readonly_select("WITH x AS (INSERT INTO sessions(session_id) VALUES('x') RETURNING session_id) SELECT * FROM x")
        assert False, "expected SQLite to reject write through read-only connection"
    except sqlite3.Error:
        pass

    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    conn.close()
