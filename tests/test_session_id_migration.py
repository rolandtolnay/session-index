import json
from pathlib import Path
import sqlite3

import pytest

import db
import migrate_session_ids as migration
from artifact_references import normalize_references
from session_identity import canonical_session_id
from indexing_lock import indexing_lock

UUID = "019f3740-9b18-7042-9a43-cf79a8e08708"
OTHER = "019f3740-9b18-7042-9a43-cf79a8e08709"


def test_reference_normalization_preserves_native_identity():
    old = f"pi:{UUID}"
    new = canonical_session_id("pi", UUID)
    text = f'''Read ~/.session-index/transcripts/{old}.tools.md and {old}.md.
Inspect tool/{old}/42 or subagent/{UUID}/0.
Parent: {UUID}
# Tool log — {old}
Raw /Users/me/.pi/agent/sessions/example_{UUID}.jsonl
Claude ~/.claude/projects/project/{UUID}.jsonl
pi --session {UUID}
Native ID: {UUID}
/project/{UUID}.md
'''
    result = normalize_references(text)
    assert f"transcripts/{new}.tools.md" in result
    assert f"tool/{new}/42" in result
    assert f"subagent/{canonical_session_id('claude', UUID)}/0" in result
    assert f"Parent: {canonical_session_id('claude', UUID)}" in result
    assert f"# Tool log — {new}" in result
    for line in text.splitlines()[4:]:
        assert line in result
    assert normalize_references(result) == result


@pytest.fixture(params=["uuid", "12hex"])
def store(tmp_path, monkeypatch, request):
    root = tmp_path / ".session-index"
    root.mkdir()
    (root / "transcripts").mkdir()
    monkeypatch.setattr(db, "DB_PATH", str(root / "sessions.db"))
    monkeypatch.setattr(db, "DATA_DIR", str(root))
    with sqlite3.connect(root / "sessions.db") as conn:
        db.init_db(conn)
        for source in ("claude", "pi", "codex"):
            old = (canonical_session_id(source, UUID)[:-4] if request.param == "12hex"
                   else UUID if source == "claude" else f"{source}:{UUID}")
            path = root / "transcripts" / f"{old}.md"
            path.write_text(f"[user] hello\n[assistant] inspect session/{old}\n")
            child = root / "transcripts" / old / "agent-1.md"
            child.parent.mkdir()
            child.write_text(f"Parent: {old}\nNative ID: {UUID}\n")
            tools = root / "transcripts" / f"{old}.tools.md"
            tools.write_text(f"# Tool log — {old}\n")
            db.upsert_session(conn, session_id=old, source=source, native_session_id=UUID,
                              summary="Keep the full summary", headline="Keep headline", transcript_path=str(path),
                              tool_log_path=str(tools), subagent_transcripts=str(child),
                              source_path=f"/raw/{source}/{UUID}.jsonl")
            for table, owner in migration.OWNERS.items():
                if table == "sessions":
                    continue
                fields = {owner: old}
                if table == "file_mutations":
                    fields["path"] = "/project/code.py"
                if table == "skill_invocations":
                    fields["skill_name"] = "test"
                conn.execute(f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(fields.values()))
            job = root / "refresh-jobs" / source / old.replace(":", "-")
            (job / "pending").mkdir(parents=True)
            (job / "pending" / "1.json").write_text(json.dumps({"source": source, "session_id": old,
                                                              "transcript_path": f"/raw/{source}/{UUID}.jsonl"}))
            (job / "state.json").write_text('{"summary": "existing state"}')
            (job / "worker.pid").write_text("12345")
        conn.commit()
    return root


def test_migration_preserves_records_and_artifacts_and_is_idempotent(store):
    before = migration.migrate(store)
    assert before["changed_sessions"] == 3
    result = migration.migrate(store, apply=True)
    assert result["changed_sessions"] == 3
    backup = Path(result["backup"])
    with sqlite3.connect(backup / "sessions.db") as before_db:
        old_claude_id = before_db.execute("SELECT session_id FROM sessions WHERE source='claude'").fetchone()[0]
    assert (backup / "transcripts" / f"{old_claude_id}.md").exists()
    assert not (store / "identity-migration.json").exists()
    with migration.connect(store / "sessions.db", readonly=True) as conn:
        for row in conn.execute("SELECT * FROM sessions"):
            assert row["session_id"] == canonical_session_id(row["source"], UUID)
            assert row["native_session_id"] == UUID
            assert row["source_path"] == f"/raw/{row['source']}/{UUID}.jsonl"
            assert row["summary"] == "Keep the full summary"
            assert Path(row["transcript_path"]).exists()
            assert Path(row["tool_log_path"]).exists()
            assert Path(row["subagent_transcripts"]).exists()
            assert f"session/{row['session_id']}" in Path(row["transcript_path"]).read_text()
            job = store / "refresh-jobs" / row["source"] / row["session_id"].replace(":", "-")
            assert json.loads((job / "pending" / "1.json").read_text())["session_id"] == row["session_id"]
            assert not (job / "worker.pid").exists()
        for table in migration.OWNERS:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 3
    assert migration.migrate(store, apply=True) == {"changed_sessions": 0, "changes": False}


@pytest.mark.parametrize("after_displacing_original", [False, True])
def test_interrupted_cutover_resumes(store, monkeypatch, after_displacing_original):
    original_replace = migration.os.replace
    interrupted = False

    def replace(source, target):
        nonlocal interrupted
        original_replace(source, target)
        at_boundary = (Path(source) == store / "transcripts") if after_displacing_original else (Path(target) == store / "transcripts")
        if at_boundary and not interrupted:
            interrupted = True
            raise OSError("simulated interruption after first component")

    monkeypatch.setattr(migration.os, "replace", replace)
    with pytest.raises(OSError, match="simulated"):
        migration.migrate(store, apply=True)
    with pytest.raises(RuntimeError, match="paused"):
        with indexing_lock(store, "pi:000000000000"):
            pytest.fail("indexing must stay disabled")
    monkeypatch.setattr(migration.os, "replace", original_replace)
    assert migration.migrate(store, apply=True)["changed_sessions"] == 3
    assert not migration.migrate(store)["changes"]


def test_interrupted_preparation_resumes_without_replacing_backup(store, monkeypatch):
    original = migration.transformed_file
    interrupted = False

    def transform(path, *args, **kwargs):
        nonlocal interrupted
        if "backup" in path.parts and not interrupted:
            interrupted = True
            raise OSError("simulated preparation interruption")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(migration, "transformed_file", transform)
    with pytest.raises(OSError, match="preparation"):
        migration.migrate(store, apply=True)
    state = json.loads((store / "identity-migration.json").read_text())
    backup_db = Path(state["run"]) / "backup" / "sessions.db"
    before = migration.file_digest(backup_db)
    monkeypatch.setattr(migration, "transformed_file", original)
    migration.migrate(store, apply=True)
    assert migration.file_digest(backup_db) == before
    assert not migration.migrate(store)["changes"]


def test_collision_is_rejected_before_any_migration_write(store, monkeypatch):
    before = sorted(p.relative_to(store) for p in (store / "transcripts").rglob("*.md"))
    monkeypatch.setattr(migration, "canonical_session_id", lambda *_: "cc:0000000000000000")
    with pytest.raises(ValueError, match="collision|identity"):
        migration.migrate(store, apply=True)
    assert not (store / "backups").exists()
    assert before == sorted(p.relative_to(store) for p in (store / "transcripts").rglob("*.md"))


def test_database_guard_preserves_owner_even_when_short_ids_collide(tmp_path):
    with sqlite3.connect(tmp_path / "test.db") as conn:
        db.init_db(conn)
        sid = canonical_session_id("pi", UUID)
        db.upsert_session(conn, session_id=sid, source="pi", native_session_id=UUID, summary="keep")
        with pytest.raises(ValueError, match="identity conflict"):
            db.upsert_session(conn, session_id=sid, source="pi", native_session_id=OTHER, summary="overwrite")
        assert conn.execute("SELECT native_session_id, summary FROM sessions").fetchone() == (UUID, "keep")
        db.upsert_session(conn, session_id=sid, summary="updated without changing identity")
        assert conn.execute("SELECT source, native_session_id FROM sessions").fetchone() == ("pi", UUID)


def test_index_collision_cannot_overwrite_artifacts(tmp_path, monkeypatch):
    import indexer
    import transcript
    from parser import ParsedSession

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(transcript, "TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    sid = canonical_session_id("claude", UUID)
    artifact = tmp_path / "transcripts" / f"{sid}.md"
    artifact.parent.mkdir()
    artifact.write_text("existing owner's transcript")
    with sqlite3.connect(db.DB_PATH) as conn:
        db.init_db(conn)
        db.upsert_session(conn, session_id=sid, source="claude", native_session_id=OTHER)
    parsed = ParsedSession(session_id=sid, native_session_id=UUID, user_message_count=1,
                           assistant_message_count=1, messages=[{"role": "user", "content": "new"}])
    with pytest.raises(ValueError, match="identity conflict"):
        indexer.index_source_transcript("claude", "/missing/raw.jsonl", indexer.NO_SUMMARY_INDEX_OPTIONS,
                                        parsed_session=parsed)
    assert artifact.read_text() == "existing owner's transcript"


def test_regeneration_expands_historical_short_references_without_touching_native_ids(store):
    import transcript

    # A 12-hex fixture persists its map; a UUID fixture needs no short-ID map.
    with sqlite3.connect(store / 'sessions.db') as conn:
        old = conn.execute("SELECT session_id FROM sessions WHERE source='pi'").fetchone()[0]
    migration.migrate(store, apply=True)
    new = canonical_session_id('pi', UUID)
    original = f"Inspect tool/{old}/42, {old}.md; native {UUID}; /raw/{UUID}.jsonl"
    rendered = transcript.render_transcript([{'role': 'assistant', 'content': original}])
    assert f'tool/{new}/42' in rendered
    assert f'{new}.md' in rendered
    assert f'native {UUID}; /raw/{UUID}.jsonl' in rendered
    assert normalize_references(rendered) == rendered


@pytest.mark.parametrize("store", ["12hex"], indirect=True)
def test_recover_short_reference_without_indexed_native_from_prior_artifacts(store):
    with sqlite3.connect(store / 'sessions.db') as conn:
        old = conn.execute("SELECT session_id FROM sessions WHERE source='pi'").fetchone()[0]
    external = canonical_session_id('codex', OTHER)
    old_external = external[:-4]
    prior = store / 'backups' / 'short-ids-prior'
    (prior / 'backup' / 'transcripts').mkdir(parents=True)
    (prior / 'manifest.json').write_text(json.dumps({'version': 1, 'root': str(store), 'mapping': {}, 'files': []}))
    (prior / 'backup' / 'transcripts' / 'example.md').write_text(f'Inspect session/codex:{OTHER}')
    path = store / 'transcripts' / f'{old}.md'
    path.write_text(f'Inspect session/{old_external}')
    migration.migrate(store, apply=True)
    new_path = store / 'transcripts' / f'{canonical_session_id("pi", UUID)}.md'
    assert new_path.read_text() == f'Inspect session/{external}'
    # The saved map supports subsequent rendering even without the old backups.
    assert normalize_references(f'session/{old_external}') == f'session/{external}'


def test_unknown_short_artifact_owner_is_not_rehashed_or_silently_left_behind(store):
    orphan = store / 'transcripts' / 'pi:000000000000.md'
    orphan.write_text('Unknown native identity')
    with pytest.raises(ValueError, match='Cannot recover native identity'):
        migration.migrate(store, apply=True)
    assert orphan.read_text() == 'Unknown native identity'


def test_refresh_rejects_old_hash_as_native_identity():
    from session_identity import refresh_session_id

    with pytest.raises(ValueError, match='not a native identity'):
        refresh_session_id('pi', canonical_session_id('pi', UUID)[:-4])
