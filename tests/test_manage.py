"""Human session management: visibility, explicit deletion, and terminal flow."""

import argparse
import curses
import json
import os
import re
from pathlib import Path
import shutil
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli
import db
import indexer
import recent_context
import tool_log
import transcript
from evidence_find import find_candidates
from manage_tui import SessionManager, clipped, plain, wrapped


@pytest.fixture
def store(tmp_path, monkeypatch):
    data = tmp_path / "data"
    artifacts = data / "transcripts"
    artifacts.mkdir(parents=True)
    monkeypatch.setattr(db, "DATA_DIR", str(data))
    for module in (db, cli):
        monkeypatch.setattr(module, "DB_PATH", str(data / "sessions.db"))
    for module in (cli, transcript, tool_log):
        monkeypatch.setattr(module, "TRANSCRIPT_DIR", str(artifacts))
    conn = db.get_connection()
    db.init_db(conn)
    yield conn, artifacts
    conn.close()


def seed(store, sid="one", **kwargs):
    conn, artifacts = store
    path = artifacts / f"{sid}.md"
    path.write_text("Generated conversation")
    fields = dict(project="project", started_at=datetime.now(timezone.utc).isoformat(),
                  headline="Implemented durable behavior", summary="Unique searchable experiment",
                  transcript_path=str(path))
    fields.update(kwargs)
    db.upsert_session(conn, session_id=sid, **fields)
    return path


def test_inventory_pages_include_hidden_and_unsummarized_not_nested(store):
    conn, _ = store
    for i in range(23):
        seed(store, f"s-{i:02}", started_at=f"2026-09-{i + 1:02}T10:00:00Z",
             summary=None, headline=None, user_messages=f"Prompt {i}")
    db.set_hidden_from_recents(conn, "s-22", True)
    seed(store, "nested", source="pi", source_path="/tmp/run-1/session.jsonl",
         started_at="2026-10-01T00:00:00Z")
    first = db.list_manage_sessions(conn)
    second = db.list_manage_sessions(conn, offset=20)
    assert [s["session_id"] for s in first] == [f"s-{i:02}" for i in range(22, 2, -1)]
    assert [s["session_id"] for s in second] == ["s-02", "s-01", "s-00"]
    assert [s["session_id"] for s in db.list_manage_sessions(conn, hidden_only=True)] == ["s-22"]


def test_hidden_excluded_from_all_context_sections_but_searchable(store, tmp_path, monkeypatch):
    conn, _ = store
    current = tmp_path / "current"
    group = tmp_path / "group"
    config = tmp_path / "groups.json"
    config.write_text(json.dumps({"version": 1, "groups": [{
        "name": "related", "projects": [str(current), str(group)], "files": ["context.md"],
    }]}))
    monkeypatch.setattr(recent_context, "PROJECT_CONTEXT_CONFIG_PATH", str(config))
    monkeypatch.setattr(recent_context, "_project_root_from_cwd", lambda _: str(current))
    now = datetime.now(timezone.utc)
    for project in ("current", "group", "other"):
        for visibility in ("visible", "hidden"):
            sid = f"{project}-{visibility}"
            seed(store, sid, project=project, project_path=str(tmp_path / project),
                 started_at=(now - timedelta(minutes=1)).isoformat(), substance_band="substantial")
            if visibility == "hidden":
                db.set_hidden_from_recents(conn, sid, True)
    context = recent_context.build_recent_context(str(current))
    for project in ("current", "group", "other"):
        assert f"{project}-visible.md" in context
        assert f"{project}-hidden.md" not in context
    assert [s["session_id"] for s in db.get_recent_by_project(conn, "current")] == ["current-visible"]
    assert len(db.get_recent_cross_project(conn, (now - timedelta(days=1)).isoformat())) == 3
    assert len(find_candidates(conn, topic="Unique searchable experiment", limit=20)["results"]) == 6
    db.set_hidden_from_recents(conn, "group-hidden", False)
    assert "group-hidden.md" in recent_context.build_recent_context(str(current))


def test_existing_database_migrates_visible_and_index_refresh_preserves_hidden(store, tmp_path):
    conn, _ = store
    seed(store)
    conn.execute("ALTER TABLE sessions DROP COLUMN hidden_from_recents")
    assert len(db.get_headlined_by_project(conn, "project")) == 1
    db.init_db(conn)
    assert db.get_session(conn, "one")["hidden_from_recents"] == 0
    source = tmp_path / "sample.jsonl"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "sample.jsonl", source)
    first = indexer.index_source_transcript("claude", str(source), indexer.NO_SUMMARY_INDEX_OPTIONS)
    db.set_hidden_from_recents(conn, first.session_id, True)
    second = indexer.index_source_transcript("claude", str(source), indexer.NO_SUMMARY_INDEX_OPTIONS)
    assert second.session_id == first.session_id
    assert db.get_session(conn, first.session_id)["hidden_from_recents"] == 1
    assert Path(second.transcript_path).exists()


def test_delete_any_session_cleans_facts_fts_artifacts_preserves_raw_and_can_reindex(store, tmp_path):
    conn, artifacts = store
    source = tmp_path / "sample.jsonl"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "sample.jsonl", source)
    original_raw = source.read_bytes()
    indexed = indexer.index_source_transcript("claude", str(source), indexer.NO_SUMMARY_INDEX_OPTIONS)
    sid = indexed.session_id
    db.upsert_session(conn, session_id=sid, summary="Valuable experiment", files_touched="production.py")
    # Exercise every owned fact table, including high-value facts that block prune.
    for table in ("tool_calls", "question_answers"):
        conn.execute(f"INSERT INTO {table}(session_id) VALUES (?)", (sid,))
    conn.execute("INSERT INTO skill_invocations(session_id, skill_name) VALUES (?, 'review')", (sid,))
    conn.execute("INSERT INTO file_mutations(session_id, path) VALUES (?, 'production.py')", (sid,))
    child_dir = artifacts / sid
    child_dir.mkdir()
    child = child_dir / "agent-child.md"
    child.write_text("child conversation")
    conn.execute("INSERT INTO subagent_runs(parent_session_id, transcript_path) VALUES (?, ?)", (sid, str(child)))
    conn.commit()
    assert cli.build_footprint_audit(conn, session_ids=[sid])["sessions"][0]["prune"]["eligible"] is False
    result = cli._delete_managed_session(conn, sid)
    assert not result["skipped_artifacts"]
    assert db.get_session(conn, sid) is None
    for table in ("tool_calls", "skill_invocations", "question_answers", "file_mutations"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (sid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM subagent_runs WHERE parent_session_id=?", (sid,)).fetchone()[0] == 0
    assert not db.find_session_candidates(conn, query="Valuable")
    assert not Path(indexed.transcript_path).exists()
    assert not Path(indexed.tool_log_path).exists()
    assert not child_dir.exists()
    assert source.read_bytes() == original_raw
    indexer.index_source_transcript("claude", str(source), indexer.NO_SUMMARY_INDEX_OPTIONS)
    assert db.get_session(conn, sid) is not None
    assert Path(indexed.transcript_path).exists()


def test_delete_retains_shared_and_external_artifacts(store, tmp_path):
    conn, artifacts = store
    external = tmp_path / "raw.jsonl"
    external.write_text("source")
    shared = seed(store, "one", tool_log_path=str(external))
    seed(store, "two", transcript_path=str(shared))
    result = cli._delete_managed_session(conn, "one")
    assert set(result["skipped_artifacts"]) == {str(shared), str(external)}
    assert shared.exists() and external.read_text() == "source"
    assert db.get_session(conn, "two") is not None
    assert not cli._is_generated_artifact_path(str(artifacts))


@pytest.mark.parametrize("reference_owner", ["session", "subagent"])
def test_delete_preserves_artifacts_referenced_through_symlinks(store, reference_owner):
    conn, artifacts = store
    shared = seed(store, "one")
    alias = artifacts / "shared-alias.md"
    alias.symlink_to(shared)
    seed(store, "two")
    if reference_owner == "session":
        db.upsert_session(conn, session_id="two", transcript_path=str(alias))
    else:
        conn.execute("INSERT INTO subagent_runs(parent_session_id, transcript_path) VALUES (?, ?)",
                     ("two", str(alias)))
        conn.commit()
    result = cli._delete_managed_session(conn, "one")
    assert str(shared) in result["skipped_artifacts"]
    assert shared.exists() and alias.read_text() == "Generated conversation"
    assert db.get_session(conn, "two") is not None


def test_delete_file_failure_keeps_database_row_for_retry(store, monkeypatch):
    conn, _ = store
    path = seed(store)
    original_remove = os.remove

    def fail(target):
        if target == str(path):
            raise PermissionError("denied")
        original_remove(target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "remove", fail)
        with pytest.raises(OSError):
            cli._delete_managed_session(conn, "one")
    assert db.get_session(conn, "one") is not None
    assert path.exists()
    cli._delete_managed_session(conn, "one")
    assert db.get_session(conn, "one") is None


def test_keyboard_hide_unhide_cancel_and_confirm_delete(store):
    conn, _ = store
    path = seed(store)
    ui = SessionManager(conn, cli._delete_managed_session)
    ui.handle_key("h")
    assert db.get_session(conn, "one")["hidden_from_recents"] == 1
    ui.handle_key("\t")
    assert len(ui.sessions) == 1
    ui.handle_key("h")
    assert db.get_session(conn, "one")["hidden_from_recents"] == 0
    assert ui.sessions == []
    ui.handle_key("\t")
    ui.handle_key("d")
    for key in "yes\n":
        ui.handle_key(key)
    assert ui.delete_target is not None
    assert path.exists()
    ui.handle_key("\x1b")
    assert ui.delete_target is None
    ui.handle_key("d")
    for key in "one\n":
        ui.handle_key(key)
    assert db.get_session(conn, "one") is None
    assert not path.exists()
    assert ui.handle_key("q") is False


def test_navigation_and_confirmation_cannot_change_target(store):
    conn, _ = store
    for i in range(23):
        seed(store, f"s-{i:02}", started_at=f"2026-09-{i + 1:02}T10:00:00Z")
    ui = SessionManager(conn, cli._delete_managed_session)
    ui.handle_key(curses.KEY_DOWN)
    assert ui.sessions[ui.selected]["session_id"] == "s-21"
    ui.handle_key(curses.KEY_RIGHT)
    assert ui.offset == 20 and ui.selected == 0
    ui.handle_key("k")
    assert ui.selected == 0
    ui.handle_key("d")
    ui.handle_key(curses.KEY_DOWN)
    ui.handle_key("h")  # Dialog input, not the hide action.
    assert ui.delete_target["session_id"] == "s-02"
    assert db.get_session(conn, "s-02")["hidden_from_recents"] == 0
    ui.handle_key("\x15")
    assert not ui.confirmation
    ui.handle_key("\x03")
    assert ui.delete_target is None
    assert db.get_session(conn, "s-02") is not None


def test_nonterminal_is_rejected_without_mutating_store(store, monkeypatch):
    conn, _ = store
    path = seed(store)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as error:
        cli.cmd_manage(argparse.Namespace())
    assert error.value.code == 2
    assert path.exists() and db.get_session(conn, "one") is not None


@pytest.mark.parametrize("columns", [60, 112])
def test_preview_paging_makes_every_word_reachable_on_short_terminals(store, columns):
    class Screen:
        def __init__(self):
            self.text = []

        def getmaxyx(self):
            return 24, columns

        def erase(self):
            self.text.clear()

        def addstr(self, y, x, text, style):
            self.text.append(text)

        def hline(self, *args):
            pass

        def refresh(self):
            pass

    conn, _ = store
    words = {f"word{i:03}" for i in range(100)}
    seed(store, summary=" ".join(sorted(words)))
    ui = SessionManager(conn, cli._delete_managed_session)
    ui.styles = dict.fromkeys(("muted", "accent", "selected", "warning", "error"), 0)
    screen = Screen()
    seen = set()
    for _ in range(100):
        ui.draw(screen)
        seen.update(re.findall(r"word\d{3}", " ".join(screen.text)))
        if ui.preview_scroll == ui.preview_max:
            break
        ui.handle_key(curses.KEY_NPAGE)
    assert words <= seen


def test_terminal_text_cannot_emit_controls_or_drop_wide_characters():
    assert "\x1b" not in plain("hello\x1b[2Jworld\nnext")
    assert clipped("你好world", 5) == "你好w"
    text = "你好世界測試"
    assert "".join(wrapped(text, 4)) == text
