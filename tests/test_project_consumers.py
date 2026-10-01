"""Project selection keeps repository membership separate from session location."""

import curses
import sqlite3
from types import SimpleNamespace

import pytest

import cli
import db
from evidence_find import find_candidates
from manage_query import ManageFilters, query_manage_sessions
from manage_tui import SessionManager
from parser import ParsedSession


@pytest.fixture
def inventory(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    canonical = tmp_path / "development" / "app"
    other = tmp_path / "reference" / "app"
    identity = f"git:{canonical}/.git"
    for sid, root, checkout in [
        ("main", canonical, canonical),
        ("linked", canonical, tmp_path / "app-feature"),
        ("independent", other, other),
    ]:
        db.upsert_session(
            conn, session_id=sid, project="app", project_id=f"git:{root}/.git",
            project_path=str(root), worktree_path=str(checkout), cwd=str(checkout / "src"),
            started_at="2026-09-01T12:00:00Z", summary="Authentication retrieval repair",
            user_messages="Authentication retrieval repair", headline="Authentication retrieval repair",
        )
        db.replace_tool_calls(conn, sid, [{
            "session_id": sid, "source": "pi", "scope": "main", "sequence": 1,
            "tool_name": "edit", "tool": "edit", "is_error": 0,
        }])
    yield conn, canonical, other, identity
    conn.close()


@pytest.mark.parametrize("criteria", [
    {}, {"topic": "authentication retrieval"}, {"tool": "edit"},
    {"topic": "authenticaton retrievel", "tool": "edit"},
])
def test_find_exact_path_spans_worktrees_without_same_name_leakage(inventory, criteria):
    conn, canonical, other, identity = inventory
    for selector in (str(canonical), identity):
        results = find_candidates(conn, project=selector, limit=10, **criteria)["results"]
        assert {r["session"]["session_id"] for r in results} == {"main", "linked"}
        assert {r["session"]["project_path"] for r in results} == {str(canonical)}
    other_results = find_candidates(conn, project=str(other), limit=10, **criteria)["results"]
    assert [r["session"]["session_id"] for r in other_results] == ["independent"]
    assert len(find_candidates(conn, project="ap", limit=10, **criteria)["results"]) == 3


def test_mixed_legacy_and_resolved_rows_have_one_exact_picker_scope(inventory):
    conn, canonical, other, identity = inventory
    db.upsert_session(conn, session_id="legacy", project="app", project_path=str(canonical))
    options = db.project_options(conn)
    assert len(options) == 2
    assert next(count for key, _label, count in options if key == identity) == 3
    expected = {"main", "linked", "legacy"}
    for selector in (identity, str(canonical)):
        assert {row["session_id"] for row in query_manage_sessions(conn, ManageFilters(project=selector)).sessions} == expected
        assert {row["session"]["session_id"] for row in find_candidates(conn, project=selector)["results"]} == expected
    ui = SessionManager(conn, lambda *_: None)
    labels = {ui.project_label(row) for row in ui.sessions if row["session_id"] in expected}
    assert len(labels) == 1
    assert sorted(count for _label, count in db.get_stats(conn)["projects"]) == [1, 3]


def test_footprint_and_manage_use_exact_project_identity(inventory):
    conn, canonical, other, identity = inventory
    audit = cli.build_footprint_audit(conn, project=str(canonical))
    assert {row["session_id"] for row in audit["sessions"]} == {"main", "linked"}
    assert len(cli.build_footprint_audit(conn, project="ap")["sessions"]) == 3
    for selector in (identity, str(canonical)):
        page = query_manage_sessions(conn, ManageFilters(project=selector))
        assert {row["session_id"] for row in page.sessions} == {"main", "linked"}
    assert query_manage_sessions(conn, ManageFilters(project="ap")).total == 0


def test_picker_disambiguates_names_and_applies_identity(inventory):
    conn, canonical, other, identity = inventory
    ui = SessionManager(conn, lambda *_: pytest.fail("No deletion expected"))
    ui.open_panel("filters")
    options = dict(ui.panel_options())
    assert len(options) == 3  # All plus two repositories, not three checkouts or one name.
    assert canonical.parent.name in options[identity]
    assert other.parent.name in options[f"git:{other}/.git"]
    assert options[identity] != options[f"git:{other}/.git"]
    # Path narrowing still selects an identity, never a same-name aggregate.
    for key in str(canonical):
        ui.handle_key(key)
    assert [value for value, _label in ui.panel_options()] == [identity]
    ui.handle_key("\n")
    assert ui.filters.project == identity and ui.total == 2
    assert options[identity] in ui.scope_label()
    assert "git:" not in ui.scope_label()
    ui.handle_key("r")
    ui.open_panel("filters")
    ui.handle_key(curses.KEY_RESIZE)
    assert ui.current_option() == identity
    assert ui.panel_options()[ui.panel_selected][0] == identity


@pytest.mark.parametrize("selector_kind", ["path", "name", "identity", "name_prefix"])
def test_backfill_restores_deleted_worktree_identity_before_selection(tmp_path, monkeypatch, capsys, selector_kind):
    import indexer
    import sources

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    canonical = str(tmp_path / "app")
    deleted = str(tmp_path / "app-feature")
    identity = f"git:{canonical}/.git"
    db.upsert_session(conn, session_id="deleted", project="app", project_id=identity,
                      project_path=canonical, cwd=deleted, worktree_path=deleted)
    parsed = ParsedSession(session_id="deleted", project="app-feature", project_id=f"dir:{deleted}",
                           project_path=deleted, cwd=deleted)
    source = sources.SourceSessionFile("claude", str(tmp_path / "deleted.jsonl"))
    monkeypatch.setattr(sources, "discover_sessions", lambda *a, **kw: [source])
    monkeypatch.setattr(indexer, "parse_session_file", lambda *a: parsed)
    seen = []

    def index(*a, parsed_session, **kw):
        seen.append(parsed_session)
        return SimpleNamespace(skipped_reason="fixture stop after project selection")

    monkeypatch.setattr(indexer, "index_source_transcript", index)
    monkeypatch.setattr(cli, "get_connection", lambda: conn)
    selectors = {"path": canonical, "name": "APP", "identity": identity, "name_prefix": "ap"}
    cli.cmd_backfill(SimpleNamespace(source="claude", project=selectors[selector_kind], force=True,
                                    prune=False, with_summary=False))
    assert parsed.project_id == identity and parsed.project_path == canonical and parsed.project == "app"
    assert parsed.cwd == deleted and parsed.worktree_path == deleted
    assert len(seen) == (0 if selector_kind == "name_prefix" else 1)
    assert "0 errors" in capsys.readouterr().out


def test_backfill_path_selector_normalizes_home_and_symlinks(tmp_path, monkeypatch):
    canonical = tmp_path / "app"
    canonical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(canonical, target_is_directory=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    session = ParsedSession(project="app", project_path=str(canonical))
    assert cli._backfill_project_matches(session, "~/alias")
    assert not cli._backfill_project_matches(session, str(tmp_path / "other"))
