"""Tests for CLI helpers."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import _print_agent_excerpts, cmd_excerpt, cmd_search


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
