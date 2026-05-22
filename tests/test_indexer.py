import argparse
import os
import shutil
import sqlite3
import sys

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


def test_cli_backfill_presets_map_to_expected_stages():
    from cli import _backfill_options

    transcripts = _backfill_options(argparse.Namespace(subagents=False, transcripts_only=True))
    assert transcripts.stages == indexer.TRANSCRIPTS_ONLY_OPTIONS.stages

    subagents = _backfill_options(argparse.Namespace(subagents=True, transcripts_only=False))
    assert subagents.stages == indexer.SUBAGENTS_ONLY_OPTIONS.stages

    combined = _backfill_options(argparse.Namespace(subagents=True, transcripts_only=True))
    assert combined.stages == frozenset({
        indexer.IndexStage.SESSION_METADATA,
        indexer.IndexStage.CLEAN_TRANSCRIPT,
        indexer.IndexStage.SUBAGENT_TRANSCRIPTS,
        indexer.IndexStage.TOOL_LOG,
    })


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

    result = indexer.index_source_transcript("claude", str(parent), indexer.SUBAGENTS_ONLY_OPTIONS)

    assert result.subagents == 0
    conn = db.get_connection()
    row = conn.execute("SELECT subagent_transcripts FROM sessions WHERE session_id = ?", (result.session_id,)).fetchone()
    conn.close()
    assert row["subagent_transcripts"] is None
