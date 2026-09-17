"""Tests for compact recent-session context shared by Claude and Pi."""

import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))

import db
import pi_context
import recent_context
import session_start
from db import init_db, upsert_session
from recent_context import _format_session, select_weekly_sessions


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _transcript(tmp_path, name):
    path = tmp_path / f"{name}.md"
    path.write_text("project\n---\n\n[user] x\n\n[assistant] useful response")
    return str(path)


def test_format_session_has_deterministic_metadata_and_path(tmp_path):
    path = _transcript(tmp_path, "pi:s1")
    result = _format_session({
        "started_at": "2026-07-28T10:00:00Z",
        "project": "dashboard-web",
        "branch": "feature/payments",
        "source": "pi",
        "headline": "Implemented payment retries for terminal provisioning",
        "transcript_path": path,
    })
    assert result == (
        "2026-07-28 dashboard-web (feature/payments) `pi:s1.md` "
        "— Implemented payment retries for terminal provisioning"
    )
    assert "[pi]" not in result
    assert str(tmp_path) not in result


def test_format_session_can_omit_branch_for_cross_project_entry(tmp_path):
    path = _transcript(tmp_path, "s-cross")
    result = _format_session({
        "started_at": "2026-07-28T10:00:00Z",
        "project": "dashboard-web",
        "branch": "feature/payments",
        "headline": "Added payment retries",
        "transcript_path": path,
    }, include_branch=False)
    assert result == "2026-07-28 dashboard-web `s-cross.md` — Added payment retries"


def test_format_session_can_omit_redundant_project(tmp_path):
    path = _transcript(tmp_path, "s1")
    result = _format_session({
        "started_at": "2026-07-28T10:00:00Z",
        "project": "session-index",
        "branch": "main",
        "headline": "Added compact session headlines",
        "transcript_path": path,
    }, include_project=False)
    assert "session-index" not in result
    assert "(main)" in result
    assert "Added compact session headlines" in result


def test_weekly_selection_prefers_substance_then_recency_and_keeps_unknown():
    def session(sid, band, started, **extra):
        return dict(session_id=sid, substance_band=band, started_at=started, **extra)

    sessions = [
        session("long-empty", "low_value", "2026-09-17T12:00:00Z", user_message_count=999),
        session("useful-new", "useful", "2026-09-17T11:00:00Z"),
        session("unknown", None, "2026-09-17T10:00:00Z"),
        session("substantial-old", "substantial", "2026-09-12T09:00:00Z"),
        session("substantial-new", "substantial", "2026-09-17T09:00:00Z"),
        session("offset", "substantial", "2026-09-17T10:00:00+02:00"),
    ]
    selected = select_weekly_sessions(sessions, limit=5)
    assert [s["session_id"] for s in selected] == [
        "substantial-new", "offset", "substantial-old", "useful-new", "unknown",
    ]
    # Do not pad with low-value material, even when there is spare capacity.
    assert select_weekly_sessions(sessions, limit=21) == selected
    assert select_weekly_sessions(sessions, limit=0) == []
    # Identical instants have a deterministic tie-break independent of input order.
    a = session("a", "useful", "2026-09-17T09:00:00Z")
    b = session("b", "useful", "2026-09-17T11:00:00+02:00")
    assert select_weekly_sessions([a, b], limit=1) == [b]


def test_group_coverage_reserves_best_representative_and_only_overflows_for_coverage():
    sessions = [dict(session_id=f"busy-{i}", project_path="/busy", substance_band="substantial",
                     started_at=f"2026-09-17T10:{i:02d}:00Z") for i in range(20)]
    # Distinct paths sharing the same basename must each be represented.
    sessions += [dict(session_id=f"quiet-{i}", project_path=f"/workspace-{i}/same-name",
                      substance_band="low_value", started_at="2026-09-11T00:00:00Z") for i in range(2)]
    selected = select_weekly_sessions(sessions, limit=14, ensure_project_coverage=True)
    assert len(selected) == 14
    assert {s["project_path"] for s in selected} == {"/busy", "/workspace-0/same-name", "/workspace-1/same-name"}
    assert "busy-0" not in {s["session_id"] for s in selected}
    assert "busy-19" in {s["session_id"] for s in selected}

    sessions += [dict(session_id=f"extra-{i}", project_path=f"/extra-{i}",
                      substance_band="useful", started_at="2026-09-11T00:00:00Z") for i in range(13)]
    selected = select_weekly_sessions(sessions, limit=14, ensure_project_coverage=True)
    assert len(selected) == 16
    assert len({s["project_path"] for s in selected}) == 16
    assert sum(s["project_path"] == "/busy" for s in selected) == 1


def test_build_recent_context_limits_filters_and_instructs(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(db, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(db, "DB_PATH", str(data_dir / "sessions.db"))
    monkeypatch.setattr(
        recent_context,
        "_project_root_from_cwd",
        lambda _cwd: str(tmp_path / "current"),
    )
    monkeypatch.setattr(
        recent_context,
        "PROJECT_CONTEXT_CONFIG_PATH",
        str(tmp_path / "missing-project-context.json"),
    )

    conn = db.get_connection()
    init_db(conn)
    now = datetime.now(timezone.utc)

    for i in range(9):
        upsert_session(
            conn,
            session_id=f"same-{i}",
            source="pi",
            project="current",
            branch=f"branch-{i}",
            started_at=(now - timedelta(hours=i)).isoformat(),
            headline=f"Implemented current project task number {i}",
            transcript_path=_transcript(tmp_path, f"same-{i}"),
        )

    for i in range(25):
        upsert_session(
            conn,
            session_id=f"cross-{i}",
            project=f"other-{i % 3}",
            branch="main",
            started_at=(now - timedelta(hours=i)).isoformat(),
            headline=f"Reviewed cross project change number {i}",
            transcript_path=_transcript(tmp_path, f"cross-{i}"),
            user_message_count=i + 1,
            assistant_message_count=i + 1,
            assistant_char_count=(i + 1) * 100,
        )

    nested_source = tmp_path / "parent-session" / "review-run" / "run-0" / "session.jsonl"
    upsert_session(
        conn,
        session_id="nested-same-project-child",
        source="pi",
        source_path=str(nested_source),
        project="current",
        branch="main",
        started_at=(now + timedelta(hours=2)).isoformat(),
        headline="Reviewed implementation as a nested child agent",
        transcript_path=_transcript(tmp_path, "nested-same-project-child"),
        user_message_count=100,
        assistant_message_count=100,
        assistant_char_count=100_000,
    )
    upsert_session(
        conn,
        session_id="nested-cross-project-child",
        source="pi",
        source_path=str(nested_source),
        project="nested-other",
        branch="main",
        started_at=(now + timedelta(hours=2)).isoformat(),
        headline="Reviewed cross project implementation as a nested child agent",
        transcript_path=_transcript(tmp_path, "nested-cross-project-child"),
        user_message_count=100,
        assistant_message_count=100,
        assistant_char_count=100_000,
    )
    upsert_session(
        conn,
        session_id="dangling",
        project="current",
        started_at=(now + timedelta(hours=1)).isoformat(),
        headline="Implemented missing transcript entry",
        transcript_path=str(tmp_path / "missing.md"),
    )
    upsert_session(
        conn,
        session_id="old-cross",
        project="old-project",
        started_at=(now - timedelta(days=8)).isoformat(),
        headline="Implemented old cross project task",
        transcript_path=_transcript(tmp_path, "old-cross"),
    )
    conn.close()

    context = recent_context.build_recent_context(str(tmp_path))
    assert context is not None
    assert f"Transcript root: {tmp_path}" in context
    assert context.count(".md`") == 28
    assert "Clean Transcript:" not in context
    assert "[pi]" not in context
    assert "## current (latest 7)" in context
    assert "## Other projects (top 21 from the last 7 days)" in context
    current_section, cross_section = context.split("## Other projects", 1)
    assert "branch-0" in current_section and "branch-6" in current_section
    assert "branch-7" not in context
    assert "(main)" not in cross_section
    assert "nested child agent" not in context
    assert "missing transcript entry" not in context
    assert "old cross project task" not in context
    assert "use the session-search skill" in context


def test_build_recent_context_surfaces_matching_project_group_separately(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "workspace"
    current_root = workspace / "synapto-current"
    current_cwd = current_root / "src"
    api_root = workspace / "synapto-api"
    duplicate_name_root = workspace / "archive" / "synapto-current"
    dashboard_root = workspace / "dashboard-web"
    current_cwd.mkdir(parents=True)

    config_path = tmp_path / "project-context.json"
    config_path.write_text(json.dumps({
        "version": 1,
        "groups": [{
            "name": "synapto-systems",
            "projects": [str(workspace / "**" / "synapto-*"), str(dashboard_root)],
            "files": ["project-context/synapto-systems.md"],
        }],
    }))
    monkeypatch.setattr(db, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(db, "DB_PATH", str(data_dir / "sessions.db"))
    monkeypatch.setattr(
        recent_context,
        "_project_root_from_cwd",
        lambda _cwd: str(current_root),
    )
    monkeypatch.setattr(recent_context, "PROJECT_CONTEXT_CONFIG_PATH", str(config_path))

    conn = db.get_connection()
    init_db(conn)
    now = datetime.now(timezone.utc)
    upsert_session(
        conn,
        session_id="current",
        project_path=str(current_root),
        project="synapto-current",
        branch="main",
        started_at=now.isoformat(),
        headline="Implemented the current project task",
        transcript_path=_transcript(tmp_path, "current"),
    )
    for i in range(75):
        upsert_session(
            conn,
            session_id=f"current-history-{i}",
            project_path=str(current_root),
            project="synapto-current",
            branch="main",
            started_at=(now + timedelta(days=100, minutes=-i)).isoformat(),
            headline=f"Implemented current project history number {i}",
            transcript_path=_transcript(tmp_path, f"current-history-{i}"),
        )

    for i in range(85):
        started_at = now - timedelta(hours=i)
        project_path = api_root if i % 2 == 0 else dashboard_root
        upsert_session(
            conn,
            session_id=f"group-{i}",
            project_path=str(project_path),
            project=project_path.name,
            branch="main",
            started_at=started_at.isoformat(),
            headline=f"Implemented grouped project task number {i}",
            substance_band="low_value" if i == 0 else "substantial" if i == 84 else "useful",
            transcript_path=_transcript(tmp_path, f"group-{i}"),
        )

    nested_source = tmp_path / "parent-session" / "review-run" / "run-0" / "session.jsonl"
    upsert_session(
        conn,
        session_id="nested-group-child",
        source="pi",
        source_path=str(nested_source),
        project_path=str(api_root),
        project="synapto-api",
        started_at=(now + timedelta(hours=2)).isoformat(),
        headline="Reviewed grouped work as a nested child agent",
        transcript_path=_transcript(tmp_path, "nested-group-child"),
    )
    upsert_session(
        conn,
        session_id="dangling-group",
        project_path=str(api_root),
        project="synapto-api",
        started_at=(now + timedelta(hours=1)).isoformat(),
        headline="Implemented grouped work with a missing transcript",
        transcript_path=str(tmp_path / "missing-group.md"),
    )

    for i in range(25):
        upsert_session(
            conn,
            session_id=f"other-{i}",
            project_path=str(tmp_path / "unrelated" / f"other-{i % 3}"),
            project=f"other-{i % 3}",
            branch="main",
            started_at=(now - timedelta(hours=i)).isoformat(),
            headline=f"Reviewed unrelated project task number {i}",
            transcript_path=_transcript(tmp_path, f"other-{i}"),
            user_message_count=i + 1,
            assistant_message_count=i + 1,
            assistant_char_count=(i + 1) * 100,
        )

    upsert_session(
        conn,
        session_id="duplicate-project-name",
        project_path=str(duplicate_name_root),
        project="synapto-current",
        branch="main",
        started_at=(now - timedelta(days=6)).isoformat(),
        headline="Implemented grouped work in a duplicate project basename",
        transcript_path=_transcript(tmp_path, "duplicate-project-name"),
    )
    conn.close()

    context = recent_context.build_recent_context(str(current_cwd))
    assert context is not None
    assert context.count(".md`") == 42
    assert "## synapto-current (latest 7)" in context
    assert "## synapto-systems group (top 14 from the last 7 days)" in context
    assert "## Other projects (top 21 from the last 7 days)" in context

    _current_section, remainder = context.split("## synapto-systems group", 1)
    group_section, other_section = remainder.split("## Other projects", 1)
    assert "duplicate project basename" in group_section
    assert "grouped project task number 0" not in group_section
    assert "grouped project task number 1" in group_section
    assert "grouped project task number 84" in group_section
    assert "grouped project task number 60" not in context
    assert "grouped project task" not in other_section
    assert "nested child agent" not in context
    assert "missing transcript" not in context
    assert "(main)" not in group_section


def test_startup_on_pre_classification_schema_keeps_all_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sessions.db"))
    current_root = tmp_path / "current"
    group_root = tmp_path / "grouped"
    monkeypatch.setattr(recent_context, "_project_root_from_cwd", lambda cwd: str(current_root))
    config = tmp_path / "project-context.json"
    config.write_text(json.dumps({"version": 1, "groups": [{
        "name": "team", "projects": [str(current_root), str(group_root)], "files": ["context.md"],
    }]}))
    monkeypatch.setattr(recent_context, "PROJECT_CONTEXT_CONFIG_PATH", str(config))
    conn = db.get_connection()
    conn.executescript(db.SCHEMA.replace("    substance_band TEXT,\n", "").replace("    substance_reason TEXT,\n", ""))
    for name, root in (("current", current_root), ("grouped", group_root), ("other", tmp_path / "other")):
        conn.execute("""INSERT INTO sessions(session_id, source, project, project_path, started_at, headline, transcript_path)
                        VALUES (?, 'claude', ?, ?, ?, 'A useful session', ?)""",
                     (name, name, str(root), datetime.now(timezone.utc).isoformat(), _transcript(tmp_path, name)))
    conn.commit()
    context = recent_context.build_recent_context(str(current_root))
    assert all(f"`{name}.md`" in context for name in ("current", "grouped", "other"))
    assert len([line for line in context.splitlines() if line.startswith("## ")]) == 3
    assert "substance_band" not in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()


def test_claude_hook_injects_shared_context(monkeypatch, capsys):
    monkeypatch.delenv("_CLAUDE_HOOK_NESTED", raising=False)
    monkeypatch.setattr(session_start, "build_recent_context", lambda cwd: f"context for {cwd}")
    monkeypatch.setattr(session_start, "log", lambda *args: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/repo"})))

    session_start.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["additionalContext"] == "context for /repo"


def test_pi_hook_prints_shared_context(monkeypatch, capsys):
    monkeypatch.setattr(pi_context, "build_recent_context", lambda cwd: f"context for {cwd}")
    monkeypatch.setattr(pi_context, "log", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["pi_context.py", "--cwd", "/repo", "--session-id", "pi:s1"])

    pi_context.main()

    assert capsys.readouterr().out.strip() == "context for /repo"
