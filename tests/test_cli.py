"""Tests for CLI helpers."""

import argparse
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli
import db
from cli import _check_integrity, cmd_find, cmd_inspect, cmd_query
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
        transcript_path=str(transcript),
        tool_log_path=str(tool_log),
    )

    issues = _check_integrity(conn)

    assert issues["orphaned_transcripts"] == []
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
