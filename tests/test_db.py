"""Tests for the SQLite + FTS5 database."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db, upsert_session, search, search_flexible, get_recent_by_project, get_stats, rebuild_fts


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
    conn.close()


def test_upsert_insert():
    conn = _make_conn()
    upsert_session(conn, session_id="test-1", project="myproject", summary="did stuff")
    row = conn.execute("SELECT * FROM sessions WHERE session_id='test-1'").fetchone()
    assert row is not None
    assert row["project"] == "myproject"
    assert row["summary"] == "did stuff"
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
    results = search(conn, "token refresh")
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
    results = search(conn, "rebuild")
    assert len(results) >= 1
    conn.close()


# ── search_flexible tests ──────────────────────────────────────────────────


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
