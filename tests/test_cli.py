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
from cli import _check_integrity, _print_agent_excerpts, cmd_excerpt, cmd_find, cmd_inspect, cmd_query, cmd_search
from db import init_db, upsert_session
from parser import ParsedToolCall
from tool_log import write_tool_log


def _write_agent_file(path, messages, header_lines=("# general-purpose — 2026-04-01 14:40", "Parent: test", "---", "")):
    lines = list(header_lines)
    for msg in messages:
        lines.append(f"[{msg['role']}] 14:40 {'─' * 30}")
        lines.append(msg["content"])
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def test_print_agent_excerpts_ranks_by_count_and_reports_remaining(tmp_path, capsys):
    # Main transcript path: <sid>.md. Agent dir: <sid>/
    main_transcript = str(tmp_path / "session.md")
    with open(main_transcript, "w") as f:
        f.write("main transcript placeholder")

    agent_dir = tmp_path / "session"
    agent_dir.mkdir()

    # High-match agent: 3 occurrences of "authentication"
    _write_agent_file(agent_dir / "agent-high.md", [
        {"role": "prompt", "content": "Research authentication patterns"},
        {"role": "agent", "content": "Found authentication docs. Authentication matters here."},
    ])
    # Low-match agent: 1 occurrence
    _write_agent_file(agent_dir / "agent-low.md", [
        {"role": "prompt", "content": "Research pagination"},
        {"role": "agent", "content": "Found one authentication mention in passing."},
    ])
    # Third matching agent: 1 occurrence — pushes remaining count to > 0
    _write_agent_file(agent_dir / "agent-other.md", [
        {"role": "prompt", "content": "Research logging"},
        {"role": "agent", "content": "Tangential note on authentication."},
    ])

    _print_agent_excerpts(main_transcript, ["authentication"])
    out = capsys.readouterr().out

    # Highest-match agent is the one shown
    assert "agent-high" in out
    assert "3 keyword hits" in out
    # Other matching agents accounted for in the footer
    assert "2 more agent transcript(s) matched" in out
    # The non-top agents should not have been printed inline
    assert "agent-low" not in out or "agent-low" in out.split("more agent transcript")[1]


def test_print_agent_excerpts_no_agent_dir_is_silent(tmp_path, capsys):
    # No <sid>/ directory next to the main transcript — early return, no output.
    main_transcript = str(tmp_path / "lonely.md")
    with open(main_transcript, "w") as f:
        f.write("main only")

    _print_agent_excerpts(main_transcript, ["anything"])
    assert capsys.readouterr().out == ""


def test_print_agent_excerpts_no_matches_is_silent(tmp_path, capsys):
    # Agent dir exists but no keyword hits — nothing to report.
    main_transcript = str(tmp_path / "session.md")
    with open(main_transcript, "w") as f:
        f.write("main")
    agent_dir = tmp_path / "session"
    agent_dir.mkdir()
    _write_agent_file(agent_dir / "agent-x.md", [
        {"role": "prompt", "content": "Unrelated topic"},
        {"role": "agent", "content": "No relevant content here."},
    ])

    _print_agent_excerpts(main_transcript, ["authentication"])
    assert capsys.readouterr().out == ""


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


def test_cmd_search_prints_tool_log_path(monkeypatch, capsys):
    monkeypatch.setattr("cli.get_connection", lambda: _DummyConn())
    monkeypatch.setattr("cli.init_db", lambda conn: None)
    monkeypatch.setattr("cli._log_search", lambda args, count, elapsed_ms: None)
    monkeypatch.setattr("cli.search_flexible", lambda *args, **kwargs: [{
        "session_id": "s1",
        "project": "proj",
        "started_at": "2026-01-01T00:00:00Z",
        "duration_seconds": 12,
        "summary": "Did work",
        "tool_log_path": "/tmp/s1.tools.md",
    }])

    cmd_search(argparse.Namespace(query="work", project=None, since=None, until=None, limit=20, any=False))
    out = capsys.readouterr().out

    assert "tool log: /tmp/s1.tools.md" in out


def test_cmd_excerpt_prints_tool_log_path(monkeypatch, tmp_path, capsys):
    transcript = tmp_path / "s1.md"
    transcript.write_text("proj | main | 2026-01-01\n---\n\n[user] ────────────────────────────────────────\nFind token\n")
    monkeypatch.setattr("cli.get_connection", lambda: _DummyConn())
    monkeypatch.setattr("cli.init_db", lambda conn: None)
    monkeypatch.setattr("cli._log_excerpt", lambda session_ids, query, elapsed_ms: None)
    monkeypatch.setattr("cli.get_session", lambda conn, ident: {
        "session_id": "s1",
        "project": "proj",
        "started_at": "2026-01-01T00:00:00Z",
        "transcript_path": str(transcript),
        "tool_log_path": "/tmp/s1.tools.md",
    })

    cmd_excerpt(argparse.Namespace(sessions=["s1"], query="token"))
    out = capsys.readouterr().out

    assert "Tool log available: /tmp/s1.tools.md" in out


# ── query (read-only escape hatch) ─────────────────────────────────────────


def _isolate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sessions.db")
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "_log_query", lambda *a, **k: None)
    return db_path


def test_cmd_query_schema_prints_tables_and_examples_without_creating_db(tmp_path, monkeypatch, capsys):
    db_path = _isolate_db(tmp_path, monkeypatch)
    cmd_query(argparse.Namespace(sql=None, json=False, limit=50, schema=True))
    out = capsys.readouterr().out
    assert "CREATE TABLE IF NOT EXISTS tool_calls" in out
    assert "CREATE TABLE IF NOT EXISTS file_mutations" in out
    assert "SELECT DISTINCT path FROM file_mutations" in out
    assert "example queries" in out
    assert not os.path.exists(db_path)


def test_cmd_query_runs_select(tmp_path, monkeypatch, capsys):
    _isolate_db(tmp_path, monkeypatch)
    conn = db.get_connection()
    init_db(conn)
    upsert_session(conn, session_id="s1", project="proj")
    db.replace_tool_calls(conn, "s1", [{
        "session_id": "s1", "source": "claude", "scope": "main", "sequence": 1,
        "timestamp": None, "tool_name": "Bash", "tool": "bash", "is_error": 0, "skill_name": None,
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
    transcript = tmp_path / "pi:abc.md"
    transcript.write_text("proj | main | 2026-05-31\n---\n\n[user] ────────────────────────────────────────\nsession index evidence\n\n[assistant] ──────────────────────────────────\nScoped evidence text.\n")
    tool_log = write_tool_log("pi:abc", [ParsedToolCall(sequence=12, tool_name="edit", arguments={"path": "etc/prd/example.md"}, result="changed")])
    upsert_session(
        conn,
        session_id="pi:abc",
        source="pi",
        project="session-index",
        started_at="2026-05-31T10:00:00Z",
        summary="Worked on session index evidence retrieval.",
        user_messages="session index evidence",
        transcript_path=str(transcript),
        tool_log_path=tool_log,
    )
    db.replace_tool_calls(conn, "pi:abc", [{
        "session_id": "pi:abc", "source": "pi", "scope": "main", "sequence": 12,
        "timestamp": None, "tool_name": "edit", "tool": "edit", "is_error": 0, "skill_name": None,
    }])
    db.replace_file_mutations(conn, "pi:abc", [{
        "session_id": "pi:abc", "source": "pi", "scope": "main", "sequence": 12,
        "timestamp": None, "tool_name": "edit", "tool": "edit", "path": "etc/prd/example.md",
    }])
    conn.close()


def test_cmd_find_emits_compact_json_candidates(tmp_path, monkeypatch, capsys):
    _seed_evidence_cli_db(tmp_path, monkeypatch)

    cmd_find(argparse.Namespace(
        topic="session index", tool=None, skill=None, mutated=None, subagent=None,
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


def test_cmd_find_mutated_ref_can_be_passed_to_inspect(tmp_path, monkeypatch, capsys):
    _seed_evidence_cli_db(tmp_path, monkeypatch)

    cmd_find(argparse.Namespace(
        topic=None, tool=None, skill=None, mutated="example.md", subagent=None,
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


def test_main_help_teaches_find_inspect_query_decision_tree(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cli.py", "--help"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "query for aggregates" in out
    assert "find" in out
    assert "inspect" in out


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
