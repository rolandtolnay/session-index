"""Tests for per-session tool log writer."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import ParsedToolCall
from subagent_parser import ParsedSubagent
from tool_log import combine_tool_calls, write_tool_log


def test_write_tool_log_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))

    assert write_tool_log("session-1", []) is None
    assert list(tmp_path.iterdir()) == []


def test_write_tool_log_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    calls = [ParsedToolCall(
        scope="main",
        sequence=1,
        timestamp="2026-01-15T10:00:13.000Z",
        tool_call_id="tool-003",
        tool_name="Bash",
        arguments={"command": "pytest"},
        result="2 passed",
        is_error=False,
    )]

    path = write_tool_log("session-1", calls, project="proj", source="claude", started_at="2026-01-15T10:00:00Z")

    assert path == str(tmp_path / "session-1.tools.md")
    content = (tmp_path / "session-1.tools.md").read_text()
    assert "# Tool log — session-1" in content
    assert "Project: proj" in content
    assert "## 001 — main — Bash — 10:00:13" in content
    assert "Status: ok" in content
    assert '"command": "pytest"' in content
    assert "2 passed" in content


def test_write_tool_log_truncates_large_result(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    result = "a" * 10_500 + "middle" + "z" * 10_500
    calls = [ParsedToolCall(sequence=1, tool_name="bash", result=result)]

    path = write_tool_log("session-1", calls)
    content = open(path).read()

    assert "[truncated: showing first 10000 and last 10000 of 21006 characters]" in content
    assert "middle" not in content


def test_combine_tool_calls_sequences_main_before_subagents():
    main = [ParsedToolCall(tool_name="read", tool_call_id="main-call")]
    sub = ParsedSubagent(agent_id="abc123", tool_calls=[
        ParsedToolCall(tool_name="bash", tool_call_id="sub-call")
    ])

    combined = combine_tool_calls(main, [sub])

    assert [(c.sequence, c.scope, c.tool_call_id) for c in combined] == [
        (1, "main", "main-call"),
        (2, "agent-abc123", "sub-call"),
    ]
