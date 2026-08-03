"""Tests for CLI helpers."""

import argparse
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli
import db
from cli import _check_integrity, cmd_find, cmd_footprint, cmd_inspect, cmd_prune, cmd_query
from db import init_db, upsert_session
from tests.evidence_helpers import seed_evidence_graph



class _DummyConn:
    def close(self):
        pass


def test_check_integrity_does_not_treat_tool_log_as_orphaned_transcript(monkeypatch, tmp_path):
    transcript = tmp_path / "s1.md"
    tool_log = tmp_path / "s1.tools.md"
    transcript.write_text("transcript")
    tool_log.write_text("tools")
    monkeypatch.setattr("cli.TRANSCRIPT_DIR", str(tmp_path))

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    upsert_session(
        conn,
        session_id="s1",
        summary="summary",
        headline="Implemented useful work",
        transcript_path=str(transcript),
        tool_log_path=str(tool_log),
    )

    issues = _check_integrity(conn)

    assert issues["orphaned_transcripts"] == []
    conn.close()


def test_check_integrity_reports_recoverable_missing_headline(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text("{}")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    upsert_session(conn, session_id="s1", summary="summary", source_path=str(source))

    issues = _check_integrity(conn)

    assert issues["missing_headline"] == ["s1"]
    assert issues["headline_recoverable"] == ["s1"]
    conn.close()


# ── query (read-only escape hatch) ─────────────────────────────────────────


def _isolate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sessions.db")
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "_log_query", lambda *a, **k: None)
    return db_path


def test_cmd_query_schema_prints_curated_reference_without_creating_db(tmp_path, monkeypatch, capsys):
    db_path = _isolate_db(tmp_path, monkeypatch)
    cmd_query(argparse.Namespace(sql=None, json=False, limit=50, schema=True))
    out = capsys.readouterr().out
    assert "Session Index query reference" in out
    assert "tool_calls" in out
    assert "skill_invocations" in out
    assert "Construct Inspection References" in out
    assert "skill/<session_id>/<sequence>" in out
    assert "SELECT DISTINCT path FROM file_mutations" in out
    assert "CREATE TABLE" not in out
    assert "--" not in out
    assert not os.path.exists(db_path)


def test_cmd_query_runs_select(tmp_path, monkeypatch, capsys):
    _isolate_db(tmp_path, monkeypatch)
    conn = db.get_connection()
    init_db(conn)
    upsert_session(conn, session_id="s1", project="proj")
    db.replace_tool_calls(conn, "s1", [{
        "session_id": "s1", "source": "claude", "scope": "main", "sequence": 1,
        "timestamp": None, "tool_name": "Bash", "tool": "bash", "is_error": 0,
    }])
    conn.close()

    cmd_query(argparse.Namespace(
        sql="SELECT tool, COUNT(*) n FROM tool_calls GROUP BY tool", json=False, limit=50, schema=False,
    ))
    out = capsys.readouterr().out
    assert "bash" in out


def _seed_evidence_cli_db(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = db.get_connection()
    init_db(conn)
    seed_evidence_graph(conn, tmp_path, write_artifacts=True)
    conn.close()


def test_cmd_find_emits_compact_json_candidates(tmp_path, monkeypatch, capsys):
    _seed_evidence_cli_db(tmp_path, monkeypatch)

    cmd_find(argparse.Namespace(
        topic="session index", tool=None, skill=None, mutated=None, mutation_mode="session", subagent=None,
        question_recommended=None, project=None, since=None, until=None, session=None, limit=2,
    ))

    data = json.loads(capsys.readouterr().out)
    result = data["results"][0]
    assert result["ref"] == "session/pi:abc"
    assert result["inspect_refs"]["primary"] == "session/pi:abc"
    assert "evidence" not in result
    # Candidate discovery remains compact and does not include transcript/tool-log evidence text.
    assert "Scoped evidence text." not in json.dumps(result)
    assert "changed" not in json.dumps(result)


def test_cmd_find_mutated_default_emits_session_ref(tmp_path, monkeypatch, capsys):
    _seed_evidence_cli_db(tmp_path, monkeypatch)

    cmd_find(argparse.Namespace(
        topic=None, tool=None, skill=None, mutated="example.md", mutation_mode="session", subagent=None,
        question_recommended=None, project=None, since=None, until=None, session=None, limit=2,
    ))

    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["ref"] == "session/pi:abc"
    assert result["inspect_refs"]["primary"] == "session/pi:abc"


def test_cmd_find_mutated_event_ref_can_be_passed_to_inspect(tmp_path, monkeypatch, capsys):
    _seed_evidence_cli_db(tmp_path, monkeypatch)

    cmd_find(argparse.Namespace(
        topic=None, tool=None, skill=None, mutated="example.md", mutation_mode="event", subagent=None,
        question_recommended=None, project=None, since=None, until=None, session=None, limit=2,
    ))
    ref = json.loads(capsys.readouterr().out)["results"][0]["ref"]

    cmd_inspect(argparse.Namespace(ref=ref, q=None, max_snippets=5))
    packet = json.loads(capsys.readouterr().out)

    assert packet["ref"] == "tool/pi:abc/12"
    assert packet["match"]["file_mutations"] == ["etc/prd/example.md"]
    assert packet["evidence"][0]["artifact"] == "tool_log"
    assert "changed" in packet["evidence"][0]["text"]


def test_cmd_inspect_invalid_ref_prints_json_error(tmp_path, monkeypatch, capsys):
    _seed_evidence_cli_db(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cmd_inspect(argparse.Namespace(ref="not/a/ref", q=None, max_snippets=5))

    assert exc.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["error"]["code"] == "invalid_ref"


def test_legacy_search_excerpt_scripts_are_removed():
    assert not os.path.exists("skills/session-search/scripts/search.py")
    assert not os.path.exists("skills/session-search/scripts/excerpt.py")


def test_cli_does_not_expose_legacy_search_excerpt_helpers():
    for name in ["cmd_search", "cmd_excerpt", "_log_search", "_log_excerpt", "_print_agent_excerpts"]:
        assert not hasattr(cli, name)


def test_main_help_teaches_find_inspect_query_decision_tree(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cli.py", "--help"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "query for aggregates" in out
    assert "find" in out
    assert "inspect" in out


def test_backfill_help_makes_summary_regeneration_opt_in(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cli.py", "backfill", "--help"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--with-summary" in out
    assert "--no-summary" not in out


def _backfill_args(**overrides):
    defaults = {
        "source": "claude",
        "session": None,
        "pi_session_dir": None,
        "codex_session_dir": None,
        "codex_archived_dir": None,
        "prune": False,
        "force": True,
        "project": None,
        "with_summary": False,
        "no_summary": False,
    }
    return SimpleNamespace(**(defaults | overrides))


def _stub_single_backfill_source(monkeypatch, tmp_path, parsed, result=None):
    import indexer
    import sources

    source_path = tmp_path / "incomplete-session.jsonl"
    source_path.write_text("{}\n")
    source_file = sources.SourceSessionFile("claude", str(source_path))
    monkeypatch.setattr(sources, "discover_sessions", lambda *args, **kwargs: [source_file])
    monkeypatch.setattr(indexer, "parse_session_file", lambda *args, **kwargs: parsed)
    if result is not None:
        monkeypatch.setattr(indexer, "index_source_transcript", lambda *args, **kwargs: result)
    else:
        monkeypatch.setattr(indexer, "index_source_transcript", lambda *args, **kwargs: pytest.fail("indexing should not run"))
    monkeypatch.setattr(cli, "get_connection", lambda: _DummyConn())
    monkeypatch.setattr(cli, "init_db", lambda _conn: None)


def test_backfill_prints_indexer_skip_reason(monkeypatch, capsys, tmp_path):
    parsed = SimpleNamespace(session_id="incomplete-session", project="project")
    result = SimpleNamespace(skipped_reason="1 user, 0 assistant msgs")
    _stub_single_backfill_source(monkeypatch, tmp_path, parsed, result)

    cli.cmd_backfill(_backfill_args())

    out = capsys.readouterr().out
    assert "skipped (1 user, 0 assistant msgs)" in out
    assert "Done: 0 processed, 1 skipped, 0 errors" in out


@pytest.mark.parametrize(("parsed", "args", "completed", "reason"), [
    (SimpleNamespace(session_id="", project="project"), _backfill_args(), set(), "missing session ID"),
    (SimpleNamespace(session_id="complete", project="project"), _backfill_args(force=False), {"complete"}, "already complete"),
    (SimpleNamespace(session_id="other-project", project="other"), _backfill_args(project="wanted"), set(), "project other does not match wanted"),
])
def test_backfill_prints_early_skip_reasons(monkeypatch, capsys, tmp_path, parsed, args, completed, reason):
    _stub_single_backfill_source(monkeypatch, tmp_path, parsed)
    monkeypatch.setattr(cli, "_completed_backfill_sessions", lambda *args, **kwargs: completed)

    cli.cmd_backfill(args)

    out = capsys.readouterr().out
    assert f"skipped ({reason})" in out
    assert "Done: 0 processed, 1 skipped, 0 errors" in out


def test_search_is_not_registered_as_primary_cli_command(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cli.py", "search", "token"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cmd_query_rejects_write(tmp_path, monkeypatch, capsys):
    _isolate_db(tmp_path, monkeypatch)
    conn = db.get_connection()
    init_db(conn)
    conn.close()

    with pytest.raises(SystemExit):
        cmd_query(argparse.Namespace(sql="DELETE FROM sessions", json=False, limit=50, schema=False))
    assert "Only SELECT" in capsys.readouterr().err


# ── footprint / prune ─────────────────────────────────────────────────────


def _isolate_artifact_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))


def test_cmd_footprint_reports_generated_artifact_size_and_keep_blockers(tmp_path, monkeypatch, capsys):
    _isolate_db(tmp_path, monkeypatch)
    _isolate_artifact_storage(tmp_path, monkeypatch)
    transcript = tmp_path / "pi:keep.md"
    tool_log = tmp_path / "pi:keep.tools.md"
    transcript.write_text("transcript")
    tool_log.write_text("tool log")

    conn = db.get_connection()
    init_db(conn)
    upsert_session(
        conn,
        session_id="pi:keep",
        source="pi",
        summary="No changes were made, but the session discussed an important decision.",
        files_touched="app.py",
        transcript_path=str(transcript),
        tool_log_path=str(tool_log),
    )
    db.replace_file_mutations(conn, "pi:keep", [{
        "session_id": "pi:keep",
        "source": "pi",
        "scope": "main",
        "sequence": 1,
        "timestamp": None,
        "tool_name": "edit",
        "tool": "edit",
        "path": "app.py",
    }])
    conn.close()

    cmd_footprint(argparse.Namespace(session=["pi:keep"], project=None, since=None, until=None, limit=20, json=True))
    data = json.loads(capsys.readouterr().out)

    session = data["sessions"][0]
    assert session["artifact_bytes"] == len("transcript") + len("tool log")
    assert session["prune"]["eligible"] is False
    assert "file_mutations_present" in session["prune"]["blocking"]
    assert "files_touched_present" in session["prune"]["blocking"]


def test_cmd_prune_dry_run_then_confirm_deletes_only_owned_generated_artifacts_and_facts(tmp_path, monkeypatch, capsys):
    _isolate_db(tmp_path, monkeypatch)
    _isolate_artifact_storage(tmp_path, monkeypatch)
    source_jsonl = tmp_path / "source.jsonl"
    source_jsonl.write_text("{}\n")
    transcript = tmp_path / "pi:low.md"
    tool_log = tmp_path / "pi:low.tools.md"
    subdir = tmp_path / "pi:low"
    subagent = subdir / "agent-child.md"
    unrelated = tmp_path / "pi:other.md"
    transcript.write_text("low transcript")
    tool_log.write_text("low tool log")
    subdir.mkdir()
    subagent.write_text("subagent")
    unrelated.write_text("other transcript")

    conn = db.get_connection()
    init_db(conn)
    upsert_session(
        conn,
        session_id="pi:low",
        source="pi",
        source_path=str(source_jsonl),
        summary="No coding or changes; no active work.",
        transcript_path=str(transcript),
        tool_log_path=str(tool_log),
        subagent_transcripts=str(subagent),
    )
    db.replace_tool_calls(conn, "pi:low", [{
        "session_id": "pi:low",
        "source": "pi",
        "scope": "main",
        "sequence": 1,
        "timestamp": None,
        "tool_name": "read",
        "tool": "read",
        "is_error": 0,
    }])
    db.replace_subagent_runs(conn, "pi:low", [])
    upsert_session(
        conn,
        session_id="pi:other",
        source="pi",
        summary="Implemented useful work.",
        transcript_path=str(unrelated),
    )
    conn.close()

    cmd_prune(argparse.Namespace(sessions=["pi:low"], confirm=False, json=True))
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["dry_run"] is True
    assert dry_run["audit"]["sessions"][0]["prune"]["eligible"] is True
    assert transcript.exists()
    assert tool_log.exists()
    assert subagent.exists()
    assert unrelated.exists()
    assert source_jsonl.exists()

    cmd_prune(argparse.Namespace(sessions=["pi:low"], confirm=True, json=True))
    confirmed = json.loads(capsys.readouterr().out)

    assert confirmed["deleted"] is True
    assert confirmed["deleted_sessions"] == 1
    assert not transcript.exists()
    assert not tool_log.exists()
    assert not subdir.exists()
    assert unrelated.exists()
    assert source_jsonl.exists()

    conn = db.get_connection()
    assert conn.execute("SELECT 1 FROM sessions WHERE session_id='pi:low'").fetchone() is None
    assert conn.execute("SELECT 1 FROM sessions WHERE session_id='pi:other'").fetchone() is not None
    assert conn.execute("SELECT COUNT(*) FROM tool_calls WHERE session_id='pi:low'").fetchone()[0] == 0
    conn.close()


def test_cmd_prune_confirm_blocks_uncertain_or_high_value_sessions(tmp_path, monkeypatch, capsys):
    _isolate_db(tmp_path, monkeypatch)
    _isolate_artifact_storage(tmp_path, monkeypatch)
    transcript = tmp_path / "pi:block.md"
    transcript.write_text("important")

    conn = db.get_connection()
    init_db(conn)
    upsert_session(
        conn,
        session_id="pi:block",
        source="pi",
        summary="No changes were made.",
        transcript_path=str(transcript),
    )
    db.replace_skill_invocations(conn, "pi:block", [{
        "session_id": "pi:block",
        "source": "pi",
        "sequence": 1,
        "timestamp": None,
        "skill_name": "review",
        "invocation_preview": None,
        "arguments": None,
        "transcript_message_index": None,
        "tool_sequence": None,
        "child_index": None,
        "subagent_transcript_path": None,
    }])
    conn.close()

    with pytest.raises(SystemExit) as exc:
        cmd_prune(argparse.Namespace(sessions=["pi:block"], confirm=True, json=True))

    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["deleted"] is False
    assert payload["blocked_sessions"] == ["pi:block"]
    assert transcript.exists()

    conn = db.get_connection()
    assert conn.execute("SELECT 1 FROM sessions WHERE session_id='pi:block'").fetchone() is not None
    assert conn.execute("SELECT COUNT(*) FROM skill_invocations WHERE session_id='pi:block'").fetchone()[0] == 1
    conn.close()


def test_cmd_prune_confirm_treats_missing_generated_artifacts_as_already_absent(tmp_path, monkeypatch, capsys):
    _isolate_db(tmp_path, monkeypatch)
    _isolate_artifact_storage(tmp_path, monkeypatch)
    source_jsonl = tmp_path / "source.jsonl"
    source_jsonl.write_text("{}\n")
    missing_transcript = tmp_path / "pi:missing.md"
    missing_tool_log = tmp_path / "pi:missing.tools.md"

    conn = db.get_connection()
    init_db(conn)
    upsert_session(
        conn,
        session_id="pi:missing",
        source="pi",
        source_path=str(source_jsonl),
        summary="No coding and no changes.",
        transcript_path=str(missing_transcript),
        tool_log_path=str(missing_tool_log),
    )
    conn.close()

    cmd_prune(argparse.Namespace(sessions=["pi:missing"], confirm=True, json=True))
    payload = json.loads(capsys.readouterr().out)

    assert payload["deleted"] is True
    artifacts = payload["audit"]["sessions"][0]["artifacts"]
    assert any(item["path"] == str(missing_transcript) and item["exists"] is False for item in artifacts)
    assert source_jsonl.exists()

    conn = db.get_connection()
    assert conn.execute("SELECT 1 FROM sessions WHERE session_id='pi:missing'").fetchone() is None
    conn.close()
