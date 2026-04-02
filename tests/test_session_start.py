"""Tests for session_start hook formatting and injection logic."""

import sqlite3
import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))

from session_start import _format_session, _format_session_short, _format_cross_project
from db import init_db, upsert_session, get_recent_by_project, get_recent_cross_project


# --- _format_session ---

def test_format_session_full():
    s = {
        "started_at": "2026-04-01T10:00:00",
        "project": "dashboard-web",
        "branch": "feature/subscriptions",
        "summary": "Implemented price catalog CRUD.",
    }
    result = _format_session(s)
    assert result == "2026-04-01 dashboard-web (feature/subscriptions) — Implemented price catalog CRUD."


def test_format_session_excludes_project():
    s = {
        "started_at": "2026-04-01T10:00:00",
        "project": "dashboard-web",
        "branch": "main",
        "summary": "Fixed login bug.",
    }
    result = _format_session(s, include_project=False)
    assert "dashboard-web" not in result
    assert result == "2026-04-01 (main) — Fixed login bug."


def test_format_session_falls_back_to_user_message():
    s = {
        "started_at": "2026-04-01T10:00:00",
        "project": "myproject",
        "branch": "main",
        "user_messages": "fix the login page\n---\nactually also the signup",
    }
    result = _format_session(s)
    assert "— fix the login page" in result
    assert "signup" not in result


def test_format_session_missing_fields():
    s = {}
    assert _format_session(s) == ""


# --- _format_session_short ---

def test_format_short_date_and_branch():
    s = {"started_at": "2026-03-31T08:00:00", "branch": "feature/multi-currency"}
    assert _format_session_short(s) == "2026-03-31 (feature/multi-currency)"


def test_format_short_no_branch():
    s = {"started_at": "2026-03-31T08:00:00"}
    assert _format_session_short(s) == "2026-03-31"


def test_format_short_ignores_summary():
    s = {
        "started_at": "2026-03-31T08:00:00",
        "branch": "main",
        "summary": "This should not appear.",
    }
    result = _format_session_short(s)
    assert "This should not appear" not in result
    assert result == "2026-03-31 (main)"


# --- _format_cross_project ---

def test_cross_project_single_project_single_branch():
    sessions = [
        {"project": "backend", "branch": "main"},
    ]
    lines = _format_cross_project(sessions)
    assert lines == ["- backend — 1 session (main)"]


def test_cross_project_single_project_multiple_sessions():
    sessions = [
        {"project": "backend", "branch": "main"},
        {"project": "backend", "branch": "main"},
        {"project": "backend", "branch": "main"},
    ]
    lines = _format_cross_project(sessions)
    assert lines == ["- backend — 3 sessions (main)"]


def test_cross_project_groups_by_project():
    sessions = [
        {"project": "frontend", "branch": "main"},
        {"project": "backend", "branch": "main"},
        {"project": "frontend", "branch": "fix/bug"},
    ]
    lines = _format_cross_project(sessions)
    assert len(lines) == 2
    assert "frontend — 2 sessions" in lines[0]
    assert "backend — 1 session" in lines[1]


def test_cross_project_two_branches_shown():
    sessions = [
        {"project": "app", "branch": "feature/a"},
        {"project": "app", "branch": "feature/b"},
    ]
    lines = _format_cross_project(sessions)
    assert lines == ["- app — 2 sessions (feature/a, feature/b)"]


def test_cross_project_caps_branches_at_two():
    sessions = [
        {"project": "app", "branch": "feature/a"},
        {"project": "app", "branch": "feature/b"},
        {"project": "app", "branch": "feature/c"},
        {"project": "app", "branch": "feature/d"},
    ]
    lines = _format_cross_project(sessions)
    assert len(lines) == 1
    assert "+2 more" in lines[0]
    assert "feature/a" in lines[0]
    assert "feature/b" in lines[0]
    assert "feature/c" not in lines[0]


def test_cross_project_three_branches_shows_plus_one():
    sessions = [
        {"project": "app", "branch": "alpha"},
        {"project": "app", "branch": "beta"},
        {"project": "app", "branch": "gamma"},
    ]
    lines = _format_cross_project(sessions)
    assert "+1 more" in lines[0]


def test_cross_project_no_branch():
    sessions = [
        {"project": "scripts"},
    ]
    lines = _format_cross_project(sessions)
    assert "(unknown)" in lines[0]


def test_cross_project_preserves_insertion_order():
    """Projects appear in the order their first session is encountered."""
    sessions = [
        {"project": "alpha", "branch": "main"},
        {"project": "beta", "branch": "main"},
        {"project": "gamma", "branch": "main"},
    ]
    lines = _format_cross_project(sessions)
    assert "alpha" in lines[0]
    assert "beta" in lines[1]
    assert "gamma" in lines[2]


# --- Integration tests: DB → query → format pipeline ---


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _ts(hours_ago: float = 0, days_ago: float = 0) -> str:
    """ISO timestamp relative to now."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago, days=days_ago)
    return dt.isoformat()


def _seed_session(conn, *, session_id, project, branch="main",
                  summary=None, started_at=None):
    upsert_session(
        conn,
        session_id=session_id,
        project=project,
        branch=branch,
        summary=summary,
        started_at=started_at or _ts(),
    )


def test_integration_tiered_same_project():
    """First 3 same-project sessions get full summaries, rest get short format."""
    conn = _make_conn()
    for i in range(5):
        _seed_session(
            conn,
            session_id=f"sp-{i}",
            project="dashboard-web",
            branch="feature/sub" if i < 3 else "main",
            summary=f"Summary number {i}",
            started_at=_ts(hours_ago=i),
        )

    same = get_recent_by_project(conn, "dashboard-web", limit=5)
    conn.close()

    lines = []
    for i, s in enumerate(same):
        if i < 3:
            lines.append(_format_session(s, include_project=False))
        else:
            lines.append(_format_session_short(s))

    # First 3 have summaries
    for line in lines[:3]:
        assert "— Summary number" in line

    # Last 2 are short (no summary text)
    for line in lines[3:]:
        assert "Summary number" not in line
        assert "(" in line  # has branch


def test_integration_cross_project_24h():
    """Cross-project query returns sessions from last 24h, grouped."""
    conn = _make_conn()
    # 3 sessions on other-project within 24h
    _seed_session(conn, session_id="cp-1", project="backend",
                  branch="main", started_at=_ts(hours_ago=2))
    _seed_session(conn, session_id="cp-2", project="backend",
                  branch="fix/bug", started_at=_ts(hours_ago=4))
    _seed_session(conn, session_id="cp-3", project="infra",
                  branch="main", started_at=_ts(hours_ago=6))
    # 1 session on current project (should be excluded)
    _seed_session(conn, session_id="cp-4", project="frontend",
                  branch="main", started_at=_ts(hours_ago=1))

    since_24h = _ts(hours_ago=24)
    cross = get_recent_cross_project(conn, since_24h,
                                     exclude_project="frontend", limit=30)
    conn.close()

    lines = _format_cross_project(cross)
    assert len(lines) == 2  # backend + infra
    # backend has 2 sessions across 2 branches
    backend_line = [l for l in lines if "backend" in l][0]
    assert "2 sessions" in backend_line
    assert "fix/bug" in backend_line
    assert "main" in backend_line
    # infra has 1 session
    infra_line = [l for l in lines if "infra" in l][0]
    assert "1 session" in infra_line


def test_integration_fallback_to_7d():
    """When 24h has no cross-project sessions, 7d window finds them."""
    conn = _make_conn()
    # Sessions from 3 days ago (outside 24h, inside 7d)
    _seed_session(conn, session_id="old-1", project="backend",
                  branch="main", started_at=_ts(days_ago=3))
    _seed_session(conn, session_id="old-2", project="infra",
                  branch="deploy/v2", started_at=_ts(days_ago=5))

    # 24h returns nothing
    since_24h = _ts(hours_ago=24)
    cross_24h = get_recent_cross_project(conn, since_24h,
                                         exclude_project="frontend", limit=30)
    assert len(cross_24h) == 0

    # 7d returns both
    since_7d = _ts(days_ago=7)
    cross_7d = get_recent_cross_project(conn, since_7d,
                                        exclude_project="frontend", limit=30)
    conn.close()

    assert len(cross_7d) == 2
    lines = _format_cross_project(cross_7d)
    assert len(lines) == 2
    projects = {l.split(" — ")[0].strip("- ") for l in lines}
    assert projects == {"backend", "infra"}


def test_integration_fallback_7d_also_empty():
    """When both 24h and 7d are empty, no cross-project section produced."""
    conn = _make_conn()
    # Only a session from 10 days ago
    _seed_session(conn, session_id="ancient-1", project="backend",
                  branch="main", started_at=_ts(days_ago=10))

    since_24h = _ts(hours_ago=24)
    cross_24h = get_recent_cross_project(conn, since_24h,
                                         exclude_project="frontend", limit=30)
    assert len(cross_24h) == 0

    since_7d = _ts(days_ago=7)
    cross_7d = get_recent_cross_project(conn, since_7d,
                                        exclude_project="frontend", limit=30)
    conn.close()

    assert len(cross_7d) == 0


def test_integration_full_output_structure():
    """End-to-end: seed DB, query, format — verify complete output."""
    conn = _make_conn()
    project = "dashboard-web"

    # 4 same-project sessions
    for i in range(4):
        _seed_session(conn, session_id=f"same-{i}", project=project,
                      branch="feature/work" if i < 2 else "main",
                      summary=f"Did task {i}",
                      started_at=_ts(hours_ago=i))
    # 3 cross-project sessions
    _seed_session(conn, session_id="cross-1", project="backend",
                  branch="main", started_at=_ts(hours_ago=1))
    _seed_session(conn, session_id="cross-2", project="backend",
                  branch="fix/x", started_at=_ts(hours_ago=2))
    _seed_session(conn, session_id="cross-3", project="infra",
                  branch="main", started_at=_ts(hours_ago=3))

    same = get_recent_by_project(conn, project, limit=5)
    since = _ts(hours_ago=24)
    cross = get_recent_cross_project(conn, since, exclude_project=project, limit=30)
    conn.close()

    # Build output exactly as main() does
    FULL_SUMMARY_COUNT = 3
    lines = ["# Recent Sessions"]
    lines.append(f"\n## {project} (last {len(same)})")
    for i, s in enumerate(same):
        if i < FULL_SUMMARY_COUNT:
            lines.append(f"- {_format_session(s, include_project=False)}")
        else:
            lines.append(f"- {_format_session_short(s)}")
    lines.append("\n## Also active (last 24h)")
    lines.extend(_format_cross_project(cross))

    output = "\n".join(lines)

    # Structure checks
    assert "# Recent Sessions" in output
    assert f"## {project} (last 4)" in output
    assert "## Also active (last 24h)" in output

    # First 3 same-project entries have summaries
    assert "— Did task 0" in output
    assert "— Did task 1" in output
    assert "— Did task 2" in output
    # 4th is short (no summary)
    assert "— Did task 3" not in output

    # Cross-project is grouped
    assert "backend — 2 sessions" in output
    assert "infra — 1 session" in output

    # No redundant project name in same-project entries
    same_section = output.split("## Also active")[0]
    # project name appears in header but not in individual entries
    entry_lines = [l for l in same_section.split("\n") if l.startswith("- ")]
    for line in entry_lines:
        assert "dashboard-web" not in line
