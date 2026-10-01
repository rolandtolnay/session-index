"""Project membership is repository-wide; session provenance stays checkout-local."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import indexer
import recent_context
from backfill_projects import apply_backfill, plan_backfill
from project_identity import resolve_project


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "initial", "--allow-empty")
    return repo


def worktree(repo, path):
    git(repo, "worktree", "add", "--quiet", "--detach", str(path))
    return path


def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    return connection


def test_common_repository_identity_and_location_boundaries(repository, tmp_path, monkeypatch):
    external = worktree(repository, tmp_path / "unrelated-name")
    nested = worktree(repository, repository / ".claude" / "worktrees" / "topic")
    clone = tmp_path / "archive" / "app"
    git(tmp_path, "clone", "--quiet", str(repository), str(clone))
    (external / "src").mkdir()
    main = resolve_project(str(repository))
    for location in (external, external / "src", nested):
        identity = resolve_project(str(location))
        assert identity.project_id == main.project_id
        assert identity.project_path == str(repository)
        assert identity.project == "app"
        assert identity.cwd == str(location)
        assert identity.worktree_path == str(nested if location == nested else external)
    assert resolve_project(str(clone)).project_id != main.project_id
    inner = repository / "independent"
    inner.mkdir()
    git(inner, "init", "-q")
    assert resolve_project(str(inner)).project_id != main.project_id
    # A lookalike directory is not a worktree, and agent Git overrides are ignored.
    ordinary = tmp_path / "app-feature-fake"
    ordinary.mkdir()
    monkeypatch.setenv("GIT_DIR", str(repository / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repository))
    assert resolve_project(str(ordinary)).project_id == f"dir:{ordinary}"
    alias = tmp_path / "alias"
    alias.symlink_to(external, target_is_directory=True)
    assert resolve_project(str(alias)).project_id == main.project_id


def test_separate_git_directory_keeps_linked_checkouts_together(tmp_path):
    repo = tmp_path / "main"
    metadata = tmp_path / "git-storage"
    git(tmp_path, "init", "-q", f"--separate-git-dir={metadata}", str(repo))
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "initial", "--allow-empty")
    other = worktree(repo, tmp_path / "other")
    main, linked = resolve_project(str(repo)), resolve_project(str(other))
    assert main.project_id == linked.project_id == f"git:{metadata}"
    assert main.project_path == linked.project_path == str(metadata)


def test_non_git_folders_are_distinct_and_failure_is_bounded(tmp_path, monkeypatch):
    first, second = tmp_path / "one" / "app", tmp_path / "two" / "app"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    assert resolve_project(str(first)).project_id != resolve_project(str(second)).project_id
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 5
        raise subprocess.TimeoutExpired(args[0], 5)
    monkeypatch.setattr("project_identity.subprocess.run", timeout)
    assert resolve_project(str(first)).project_path == str(first)


@pytest.mark.parametrize("source,fixture", [("claude", "sample.jsonl"), ("pi", "pi_sample.jsonl"), ("codex", "codex_sample.jsonl")])
def test_indexing_and_reindex_after_worktree_removal(repository, tmp_path, monkeypatch, source, fixture):
    checkout = worktree(repository, tmp_path / "app-feature")
    location = checkout / "src"
    location.mkdir()
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", str(data))
    monkeypatch.setattr(db, "DB_PATH", str(data / "sessions.db"))
    entries = [json.loads(line) for line in (Path(__file__).parent / "fixtures" / fixture).read_text().splitlines() if line.strip()]
    for entry in entries:
        if "cwd" in entry:
            entry["cwd"] = str(location)
        if isinstance(entry.get("payload"), dict) and "cwd" in entry["payload"]:
            entry["payload"]["cwd"] = str(location)
    path = tmp_path / fixture
    path.write_text("\n".join(json.dumps(entry) for entry in entries))
    indexed = indexer.index_fast(source, str(path))
    assert not indexed.skipped_reason
    connection = db.get_connection()
    before = db.get_session(connection, indexed.session_id)
    assert before["project_id"] == resolve_project(str(repository)).project_id
    assert before["project_path"] == str(repository)
    assert before["cwd"] == str(location)
    assert before["worktree_path"] == str(checkout)
    git(repository, "worktree", "remove", "--force", str(checkout))
    reparsed = indexer.parse_session_file(source, str(path))
    assert reparsed.project_id.startswith("dir:")
    indexer.index_fast(source, str(path))
    after = db.get_session(connection, indexed.session_id)
    for field in ("session_id", "project_id", "project_path", "project", "cwd", "worktree_path", "files_touched", "source_path"):
        assert after[field] == before[field], field
    connection.close()


def test_metadata_backfill_preserves_history_and_is_idempotent(repository, tmp_path):
    removed = worktree(repository, tmp_path / "app-fix-old")
    connection = conn()
    for sid, path in (("main", repository), ("old", removed), ("unrelated", tmp_path / "reference" / "app")):
        db.upsert_session(connection, session_id=sid, project_path=str(path), project=path.name,
                          summary="Original summary", headline="Original headline", source_path="/source/unchanged",
                          files_touched="/old/worktree/file.py", transcript_path="/transcripts/unchanged.md")
    db.replace_file_mutations(connection, "old", [{"session_id": "old", "path": str(removed / "file.py")}])
    git(repository, "worktree", "remove", str(removed))
    before = {row["session_id"]: dict(row) for row in connection.execute("SELECT * FROM sessions")}
    mapping = {str(removed): str(repository)}
    plan = plan_backfill(connection, mapping)
    assert len(plan) == 3
    apply_backfill(connection, plan)
    assert plan_backfill(connection, mapping) == []
    after = {row["session_id"]: dict(row) for row in connection.execute("SELECT * FROM sessions")}
    for sid in before:
        for field in before[sid].keys() - {"project_id", "project", "project_path", "worktree_path"}:
            assert before[sid][field] == after[sid][field], (sid, field)
    assert after["old"]["project_id"] == after["main"]["project_id"]
    assert after["old"]["worktree_path"] == str(removed)
    assert after["old"]["cwd"] is None  # Historical starting subdirectory is unknown.
    assert after["unrelated"]["project_id"] != after["main"]["project_id"]
    assert connection.execute("SELECT path FROM file_mutations").fetchone()[0] == str(removed / "file.py")
    assert len(db.find_session_candidates(connection, query="app", project=str(repository))) == 2
    # Even an overwrite-all metadata upsert cannot undo the reviewed correction.
    db.upsert_session(connection, session_id="old", project_id=f"dir:{removed}", project_path=str(removed),
                      project=removed.name, overwrite_fields={"project_id", "project_path", "project", "worktree_path"})
    assert db.get_session(connection, "old")["project_id"] == after["main"]["project_id"]


def test_backfill_rejects_repository_merge_and_rolls_back_stale_plan(repository, tmp_path):
    clone = tmp_path / "app-functions"
    git(tmp_path, "clone", "--quiet", str(repository), str(clone))
    connection = conn()
    db.upsert_session(connection, session_id="a", project_path=str(repository), project="app")
    db.upsert_session(connection, session_id="b", project_path=str(clone), project="app-functions")
    with pytest.raises(ValueError, match="independent Git repositories"):
        plan_backfill(connection, {str(clone): str(repository)})
    plan = plan_backfill(connection, {})
    connection.execute("UPDATE sessions SET project='changed concurrently' WHERE session_id='b'")
    connection.commit()
    with pytest.raises(ValueError, match="rolled back"):
        apply_backfill(connection, plan)
    assert connection.execute("SELECT project_id FROM sessions WHERE session_id='a'").fetchone()[0] is None


def test_recent_context_inherits_groups_without_duplicate_project_sections(repository, tmp_path, monkeypatch):
    checkout = worktree(repository, tmp_path / "elsewhere" / "feature")
    clone = tmp_path / "archive" / "app"
    git(tmp_path, "clone", "--quiet", str(repository), str(clone))
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    external_sibling = checkout.parent / "sibling"
    external_sibling.mkdir()
    config = tmp_path / "groups.json"
    config.write_text(json.dumps({"version": 1, "groups": [
        {"name": "team", "projects": [str(repository), str(sibling)], "files": ["context.md"]},
        {"name": "checkout-only", "projects": [str(checkout.parent)], "files": ["context.md"]},
    ]}))
    monkeypatch.setattr(recent_context, "PROJECT_CONTEXT_CONFIG_PATH", str(config))
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sessions.db"))
    connection = db.get_connection()
    db.init_db(connection)
    for sid, path in (("main", repository), ("linked", checkout), ("clone", clone), ("sibling", sibling), ("external-sibling", external_sibling)):
        identity = resolve_project(str(path))
        transcript = tmp_path / f"{sid}.md"
        transcript.write_text("conversation")
        db.upsert_session(connection, session_id=sid, project_id=identity.project_id,
                          project_path=identity.project_path, project=identity.project,
                          worktree_path=identity.worktree_path, cwd=identity.cwd,
                          started_at=datetime.now(timezone.utc).isoformat(),
                          headline=f"Session {sid}", transcript_path=str(transcript))
    connection.close()
    context = recent_context.build_recent_context(str(checkout))
    assert context == recent_context.build_recent_context(str(repository))
    assert "## checkout-only" not in context
    external_context = recent_context.build_recent_context(str(external_sibling))
    assert "`linked.md`" in external_context.split("## Other projects", 1)[1]
    same, rest = context.split("## team group", 1)
    grouped, other = rest.split("## Other projects", 1)
    assert "`main.md`" in same and "`linked.md`" in same
    assert "`sibling.md`" in grouped and "`clone.md`" in other
    for sid in ("main", "linked", "sibling", "clone"):
        assert context.count(f"`{sid}.md`") == 1
