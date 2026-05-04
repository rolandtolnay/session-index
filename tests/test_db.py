"""Tests for the SQLite + FTS5 database."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db, upsert_session, search_flexible, get_session, get_recent_by_project, get_stats, rebuild_fts, _build_fts_query


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
    upsert_session(conn, session_id="test-2", project="proj", summary="first summary")
    # Update without summary — should preserve existing
    upsert_session(conn, session_id="test-2", branch="feature-x")
    row = conn.execute("SELECT * FROM sessions WHERE session_id='test-2'").fetchone()
    assert row["summary"] == "first summary"
    assert row["branch"] == "feature-x"
    conn.close()


def test_upsert_overwrites_with_value():
    conn = _make_conn()
    upsert_session(conn, session_id="test-3", summary="old")
    upsert_session(conn, session_id="test-3", summary="new")
    row = conn.execute("SELECT * FROM sessions WHERE session_id='test-3'").fetchone()
    assert row["summary"] == "new"
    conn.close()


def test_search_fts():
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
    results = search_flexible(conn, query="token refresh")
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
    results = search_flexible(conn, query="rebuild")
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


# ── search_flexible tests ──────────────────────────────────────────────────


def test_search_flexible_or_mode():
    conn = _make_conn()
    upsert_session(conn, session_id="or1", project="proj",
                   user_messages="auth token refresh", summary="Fixed auth tokens")
    upsert_session(conn, session_id="or2", project="proj",
                   user_messages="add pagination", summary="Added pagination")
    # AND: only or1 matches (has both "auth" and "token")
    results_and = search_flexible(conn, query="auth pagination", use_or=False)
    assert len(results_and) == 0  # no session has both
    # OR: both match (or1 has "auth", or2 has "pagination")
    results_or = search_flexible(conn, query="auth pagination", use_or=True)
    assert len(results_or) == 2
    conn.close()


def test_search_flexible_fts_only():
    conn = _make_conn()
    upsert_session(conn, session_id="sf1", project="proj",
                   user_messages="auth token refresh", summary="Fixed auth tokens")
    upsert_session(conn, session_id="sf2", project="proj",
                   user_messages="add pagination", summary="Added pagination")
    results = search_flexible(conn, query="auth token")
    assert len(results) >= 1
    assert any(r["session_id"] == "sf1" for r in results)
    assert not any(r["session_id"] == "sf2" for r in results)
    conn.close()


def test_search_flexible_project_prefix():
    conn = _make_conn()
    upsert_session(conn, session_id="sp1", project="synapto-backend",
                   started_at="2026-03-15T00:00:00Z")
    upsert_session(conn, session_id="sp2", project="synapto-web",
                   started_at="2026-03-16T00:00:00Z")
    upsert_session(conn, session_id="sp3", project="dashboard-web",
                   started_at="2026-03-17T00:00:00Z")
    results = search_flexible(conn, project="synapto")
    assert len(results) == 2
    ids = {r["session_id"] for r in results}
    assert ids == {"sp1", "sp2"}
    conn.close()


def test_search_flexible_date_range():
    conn = _make_conn()
    upsert_session(conn, session_id="sd1", project="p",
                   started_at="2026-02-15T10:00:00Z")
    upsert_session(conn, session_id="sd2", project="p",
                   started_at="2026-03-15T10:00:00Z")
    upsert_session(conn, session_id="sd3", project="p",
                   started_at="2026-04-15T10:00:00Z")
    results = search_flexible(conn, since="2026-03-01", until="2026-03-31")
    assert len(results) == 1
    assert results[0]["session_id"] == "sd2"
    conn.close()


def test_search_flexible_combined():
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
    results = search_flexible(conn, query="debug auth",
                              project="dashboard", since="2026-03-01")
    assert len(results) == 1
    assert results[0]["session_id"] == "sc1"
    conn.close()


def test_search_flexible_no_filters():
    conn = _make_conn()
    upsert_session(conn, session_id="sn1", project="a",
                   started_at="2026-03-01T00:00:00Z")
    upsert_session(conn, session_id="sn2", project="b",
                   started_at="2026-03-10T00:00:00Z")
    upsert_session(conn, session_id="sn3", project="c",
                   started_at="2026-03-05T00:00:00Z")
    results = search_flexible(conn, limit=3)
    assert len(results) == 3
    # Most recent first
    assert results[0]["session_id"] == "sn2"
    assert results[1]["session_id"] == "sn3"
    assert results[2]["session_id"] == "sn1"
    conn.close()


def test_search_flexible_until_inclusive():
    conn = _make_conn()
    upsert_session(conn, session_id="su1", project="p",
                   started_at="2026-03-31T23:30:00Z")
    upsert_session(conn, session_id="su2", project="p",
                   started_at="2026-04-01T00:30:00Z")
    # Bare date until should include full day
    results = search_flexible(conn, until="2026-03-31")
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
