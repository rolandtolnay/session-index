import argparse
import json
import os
import shutil
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import indexer

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
SAMPLE = os.path.join(FIXTURES, "sample.jsonl")
SUB_JSONL = os.path.join(FIXTURES, "subagent_explore.jsonl")
SUB_META = os.path.join(FIXTURES, "subagent_explore.meta.json")


def _isolate_storage(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    transcript_dir = data_dir / "transcripts"
    monkeypatch.setattr(db, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(db, "DB_PATH", str(data_dir / "sessions.db"))

    import transcript
    import tool_log

    monkeypatch.setattr(transcript, "TRANSCRIPT_DIR", str(transcript_dir))
    monkeypatch.setattr(tool_log, "TRANSCRIPT_DIR", str(transcript_dir))
    return transcript_dir


def _copy_parent(tmp_path, name="sample.jsonl"):
    parent = tmp_path / name
    shutil.copyfile(SAMPLE, parent)
    return parent


def _add_subagent(parent_path):
    subdir = parent_path.parent / parent_path.stem / "subagents"
    subdir.mkdir(parents=True)
    shutil.copyfile(SUB_JSONL, subdir / "agent-a5f64306c4e829331.jsonl")
    shutil.copyfile(SUB_META, subdir / "agent-a5f64306c4e829331.meta.json")


def test_index_fast_delegates_to_staged_metadata_only(monkeypatch):
    calls = []

    def fake_index_source_transcript(source, path, options):
        calls.append((source, path, options))
        return indexer.IndexResult(session_id="s")

    monkeypatch.setattr(indexer, "index_source_transcript", fake_index_source_transcript)

    result = indexer.index_fast("claude", "/tmp/session.jsonl")

    assert result.session_id == "s"
    assert calls == [("claude", "/tmp/session.jsonl", indexer.FAST_INDEX_OPTIONS)]
    assert calls[0][2].stages == frozenset({indexer.IndexStage.SESSION_METADATA})


def test_full_index_writes_summary_transcript_tool_log_and_subagent_paths(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    monkeypatch.setattr("summarizer.summarize", lambda **kwargs: "summary text")
    parent = _copy_parent(tmp_path)
    _add_subagent(parent)

    result = indexer.index_source_transcript("claude", str(parent), indexer.FULL_INDEX_OPTIONS)

    assert result.summary_generated is True
    assert result.transcript_path and os.path.exists(result.transcript_path)
    assert result.tool_log_path and os.path.exists(result.tool_log_path)
    assert result.subagents == 1

    conn = db.get_connection()
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (result.session_id,)).fetchone()
    conn.close()
    assert row["summary"] == "summary text"
    assert row["transcript_path"] == result.transcript_path
    assert row["tool_log_path"] == result.tool_log_path
    assert row["subagent_transcripts"] and "agent-a5f64306c4e829331.md" in row["subagent_transcripts"]


def test_full_index_populates_tool_and_subagent_fact_tables(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    monkeypatch.setattr("summarizer.summarize", lambda **kwargs: "summary text")
    parent = _copy_parent(tmp_path)
    _add_subagent(parent)

    result = indexer.index_source_transcript("claude", str(parent), indexer.FULL_INDEX_OPTIONS)
    sid = result.session_id

    conn = db.get_connection()
    tool_calls = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE session_id=?", (sid,)).fetchone()[0]
    scopes = {r[0] for r in conn.execute("SELECT DISTINCT scope FROM tool_calls WHERE session_id=?", (sid,))}
    runs = conn.execute("SELECT COUNT(*) FROM subagent_runs WHERE parent_session_id=?", (sid,)).fetchone()[0]
    conn.close()

    # 4 parent (Bash, Edit, Edit, Read) + 3 subagent (Bash, Grep, Read)
    assert tool_calls == 7
    assert "main" in scopes
    assert any(s.startswith("agent-") for s in scopes)
    assert runs == 1  # one discovered subagent artifact (no Agent request in parent)


def test_full_index_populates_file_mutations_idempotently(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    monkeypatch.setattr("summarizer.summarize", lambda **kwargs: "summary text")
    parent = _copy_parent(tmp_path)
    _add_subagent(parent)

    result = indexer.index_source_transcript("claude", str(parent), indexer.FULL_INDEX_OPTIONS)
    sid = result.session_id

    conn = db.get_connection()
    file_mutations = conn.execute(
        "SELECT scope, tool_name, tool, path FROM file_mutations WHERE session_id=? ORDER BY sequence, rowid",
        (sid,),
    ).fetchall()
    conn.close()
    assert [tuple(row) for row in file_mutations] == [
        ("main", "Edit", "edit", "/Users/test/project/auth.py"),
        ("main", "Edit", "edit", "/Users/test/project/auth.py"),
    ]

    # Re-index must not duplicate (delete-then-insert).
    indexer.index_source_transcript("claude", str(parent), indexer.FULL_INDEX_OPTIONS)
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM file_mutations WHERE session_id=?", (sid,)).fetchone()[0] == 2
    conn.close()


def test_no_summary_index_populates_skill_invocations_idempotently(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    parent = _copy_parent(tmp_path)

    result = indexer.index_source_transcript("claude", str(parent), indexer.NO_SUMMARY_INDEX_OPTIONS)

    conn = db.get_connection()
    names = [row[0] for row in conn.execute(
        "SELECT skill_name FROM skill_invocations WHERE session_id=? ORDER BY sequence",
        (result.session_id,),
    )]
    conn.close()
    assert names == ["verify", "analyze-problem"]

    indexer.index_source_transcript("claude", str(parent), indexer.NO_SUMMARY_INDEX_OPTIONS)
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM skill_invocations WHERE session_id=?", (result.session_id,)).fetchone()[0] == 2
    conn.close()


def test_no_summary_index_populates_file_mutations(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    parent = _copy_parent(tmp_path)

    result = indexer.index_source_transcript("claude", str(parent), indexer.NO_SUMMARY_INDEX_OPTIONS)

    conn = db.get_connection()
    paths = [row[0] for row in conn.execute(
        "SELECT path FROM file_mutations WHERE session_id=? ORDER BY sequence, rowid",
        (result.session_id,),
    )]
    conn.close()
    assert paths == ["/Users/test/project/auth.py", "/Users/test/project/auth.py"]


def test_file_mutations_include_subagent_scope_when_subagents_are_parsed(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    parent = _copy_parent(tmp_path)
    _add_subagent(parent)
    subagent_path = parent.parent / parent.stem / "subagents" / "agent-a5f64306c4e829331.jsonl"
    with open(subagent_path, "a") as f:
        f.write("\n" + json.dumps({
            "parentUuid": "uuid-sa-009",
            "isSidechain": True,
            "agentId": "a5f64306c4e829331",
            "type": "assistant",
            "message": {"role": "assistant", "model": "claude-haiku-4-5-20251001", "content": [
                {"type": "tool_use", "id": "tool-sa-edit", "name": "Edit", "input": {"file_path": "/Users/test/project/agent.py", "old_string": "a", "new_string": "b"}},
            ]},
            "uuid": "uuid-sa-010",
            "timestamp": "2026-01-15T10:00:11.000Z",
            "sessionId": "parent-session-123",
        }))
        f.write("\n" + json.dumps({
            "parentUuid": "uuid-sa-010",
            "isSidechain": True,
            "agentId": "a5f64306c4e829331",
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tool-sa-edit", "content": "ok", "is_error": False},
            ]},
            "uuid": "uuid-sa-011",
            "timestamp": "2026-01-15T10:00:12.000Z",
            "sessionId": "parent-session-123",
        }))

    result = indexer.index_source_transcript("claude", str(parent), indexer.NO_SUMMARY_INDEX_OPTIONS)

    conn = db.get_connection()
    row = conn.execute(
        "SELECT scope, tool_name, path FROM file_mutations WHERE session_id=? AND path=?",
        (result.session_id, "/Users/test/project/agent.py"),
    ).fetchone()
    conn.close()
    assert tuple(row) == ("agent-a5f64306c4e829331", "Edit", "/Users/test/project/agent.py")


def test_index_db_write_rolls_back_session_when_fact_persistence_fails(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    parent = _copy_parent(tmp_path)

    def fail_replace_tool_calls(*args, **kwargs):
        raise RuntimeError("fact write failed")

    monkeypatch.setattr(db, "replace_tool_calls", fail_replace_tool_calls)

    with pytest.raises(RuntimeError, match="fact write failed"):
        indexer.index_source_transcript("claude", str(parent), indexer.NO_SUMMARY_INDEX_OPTIONS)

    parsed = indexer.parse_session_file("claude", str(parent))
    conn = db.get_connection()
    row = conn.execute("SELECT session_id FROM sessions WHERE session_id=?", (parsed.session_id,)).fetchone()
    facts = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE session_id=?", (parsed.session_id,)).fetchone()[0]
    conn.close()
    assert row is None
    assert facts == 0


def test_file_mutation_failure_rolls_back_session_and_prior_facts(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    parent = _copy_parent(tmp_path)

    def fail_replace_file_mutations(*args, **kwargs):
        raise RuntimeError("file mutation write failed")

    monkeypatch.setattr(db, "replace_file_mutations", fail_replace_file_mutations)

    with pytest.raises(RuntimeError, match="file mutation write failed"):
        indexer.index_source_transcript("claude", str(parent), indexer.NO_SUMMARY_INDEX_OPTIONS)

    parsed = indexer.parse_session_file("claude", str(parent))
    conn = db.get_connection()
    row = conn.execute("SELECT session_id FROM sessions WHERE session_id=?", (parsed.session_id,)).fetchone()
    tool_calls = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE session_id=?", (parsed.session_id,)).fetchone()[0]
    mutations = conn.execute("SELECT COUNT(*) FROM file_mutations WHERE session_id=?", (parsed.session_id,)).fetchone()[0]
    conn.close()
    assert row is None
    assert tool_calls == 0
    assert mutations == 0


def test_metadata_only_index_does_not_write_fact_tables(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    parent = _copy_parent(tmp_path)

    result = indexer.index_source_transcript("claude", str(parent), indexer.FAST_INDEX_OPTIONS)

    conn = db.get_connection()
    n = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE session_id=?", (result.session_id,)).fetchone()[0]
    skills = conn.execute("SELECT COUNT(*) FROM skill_invocations WHERE session_id=?", (result.session_id,)).fetchone()[0]
    mutations = conn.execute("SELECT COUNT(*) FROM file_mutations WHERE session_id=?", (result.session_id,)).fetchone()[0]
    conn.close()
    assert n == 0  # fact tables track the tool-log stage, absent here
    assert skills == 0
    assert mutations == 0


def test_pi_question_answer_recovered_into_fact_table(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    monkeypatch.setattr("pi_parser._git_branch", lambda cwd: "main")
    fixture = os.path.join(FIXTURES, "pi_question.jsonl")

    result = indexer.index_source_transcript("pi", fixture, indexer.NO_SUMMARY_INDEX_OPTIONS)

    conn = db.get_connection()
    row = conn.execute(
        "SELECT selected_label, was_recommended, is_other, multi_select, option_count "
        "FROM question_answers WHERE session_id=?",
        (result.session_id,),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["selected_label"] == "Future + existing"
    assert row["was_recommended"] == 0  # recommended option was "Future only (Recommended)"
    assert row["is_other"] == 0
    assert row["multi_select"] == 0
    assert row["option_count"] == 2


def test_cli_backfill_options_select_pass():
    from cli import _backfill_options

    default = _backfill_options(argparse.Namespace(with_summary=False, no_summary=False))
    assert default.stages == indexer.NO_SUMMARY_INDEX_OPTIONS.stages
    # Default drops only the LLM summary; deterministic artifacts + fact tables remain.
    assert indexer.IndexStage.SUMMARY not in default.stages
    assert indexer.IndexStage.CLEAN_TRANSCRIPT in default.stages
    assert indexer.IndexStage.SUBAGENT_TRANSCRIPTS in default.stages
    assert indexer.IndexStage.TOOL_LOG in default.stages

    with_summary = _backfill_options(argparse.Namespace(with_summary=True, no_summary=False))
    assert with_summary.stages == indexer.FULL_INDEX_OPTIONS.stages
    assert indexer.IndexStage.SUMMARY in with_summary.stages

    deprecated_no_summary = _backfill_options(argparse.Namespace(with_summary=False, no_summary=True))
    assert deprecated_no_summary.stages == indexer.NO_SUMMARY_INDEX_OPTIONS.stages


def test_summary_stage_preserves_old_summary_when_generation_fails(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    monkeypatch.setattr("summarizer.summarize", lambda **kwargs: None)
    parent = _copy_parent(tmp_path, "summary-failure.jsonl")
    parsed = indexer.parse_session_file("claude", str(parent))

    conn = db.get_connection()
    db.init_db(conn)
    db.upsert_session(conn, session_id=parsed.session_id, summary="old summary")
    conn.close()

    result = indexer.index_source_transcript("claude", str(parent), indexer.FULL_INDEX_OPTIONS, parsed_session=parsed)

    assert result.summary_generated is False
    conn = db.get_connection()
    row = conn.execute("SELECT summary FROM sessions WHERE session_id = ?", (parsed.session_id,)).fetchone()
    conn.close()
    assert row["summary"] == "old summary"


def test_requested_artifact_stage_can_clear_old_owned_field(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    parent = _copy_parent(tmp_path, "no-subagents.jsonl")

    parsed = indexer.parse_session_file("claude", str(parent))
    conn = db.get_connection()
    db.init_db(conn)
    db.upsert_session(
        conn,
        session_id=parsed.session_id,
        subagent_transcripts="/old/agent.md",
        tool_log_path="/old/tools.md",
    )
    conn.close()

    result = indexer.index_source_transcript("claude", str(parent), indexer.NO_SUMMARY_INDEX_OPTIONS)

    assert result.subagents == 0
    conn = db.get_connection()
    row = conn.execute("SELECT subagent_transcripts FROM sessions WHERE session_id = ?", (result.session_id,)).fetchone()
    conn.close()
    assert row["subagent_transcripts"] is None
