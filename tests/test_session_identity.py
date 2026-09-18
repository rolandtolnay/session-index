"""Runtime identity and historical-reference safeguards, independent of migration."""

import json
import sqlite3

import pytest

import db
from artifact_references import normalize_references
from session_identity import canonical_session_id

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


@pytest.mark.parametrize("old_format", ["uuid", "12hex"])
def test_regeneration_expands_historical_references_without_touching_native_ids(tmp_path, monkeypatch, old_format):
    import transcript

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sessions.db"))
    new = canonical_session_id("pi", UUID)
    old = f"pi:{UUID}" if old_format == "uuid" else new[:-4]
    if old_format == "12hex":
        # The saved mapping is runtime data: no migration, database, or backup
        # is needed to render old references, even for an unindexed session.
        (tmp_path / "reference-ids.json").write_text(json.dumps({old: new}))
    unknown = "pi:000000000000"
    original = f"Inspect tool/{old}/42, {old}.md; native {UUID}; /raw/{UUID}.jsonl; session/{unknown}"
    rendered = transcript.render_transcript([{"role": "assistant", "content": original}])
    assert f"tool/{new}/42" in rendered
    assert f"{new}.md" in rendered
    assert f"native {UUID}; /raw/{UUID}.jsonl" in rendered
    assert f"session/{unknown}" in rendered
    assert normalize_references(rendered) == rendered


def test_refresh_rejects_old_hash_as_native_identity():
    from session_identity import refresh_session_id

    with pytest.raises(ValueError, match="not a native identity"):
        refresh_session_id("pi", canonical_session_id("pi", UUID)[:-4])
