"""Tests for the JSONL conversation parser."""

import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import parse_jsonl, _format_tool_use, _format_bash_result, _extract_user_text

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.jsonl")


def test_parse_session_metadata():
    session = parse_jsonl(FIXTURE)
    assert session.session_id == "test-session-abc123"
    assert session.slug == "fixing-login-bug"
    assert session.branch == "main"
    assert session.model == "claude-sonnet-4-5-20250514"
    assert session.project == "project"


def test_parse_user_messages():
    session = parse_jsonl(FIXTURE)
    # Should have 3 actual user messages (not tool_result-only entries)
    assert session.user_message_count == 3
    assert "Fix the login bug in auth.py" in session.user_messages[0]
    assert "empty strings" in session.user_messages[1]
    assert "Perfect" in session.user_messages[2]


def test_parse_files_touched():
    session = parse_jsonl(FIXTURE)
    assert "/Users/test/project/auth.py" in session.files_touched


def test_parse_tools_used():
    session = parse_jsonl(FIXTURE)
    assert "Edit" in session.tools_used
    assert "Read" in session.tools_used
    assert "Bash" in session.tools_used


def test_parse_timestamps():
    session = parse_jsonl(FIXTURE)
    assert session.started_at.startswith("2026-01-15")
    assert session.ended_at.startswith("2026-01-15")
    assert session.duration_seconds > 0


def test_parse_messages_list():
    session = parse_jsonl(FIXTURE)
    # Messages should alternate user/assistant, skipping tool-result-only entries
    assert len(session.messages) > 0
    roles = [m["role"] for m in session.messages]
    assert "user" in roles
    assert "assistant" in roles


def test_format_tool_use():
    assert _format_tool_use({"name": "Read", "input": {"file_path": "/foo/bar.py"}}) == "[Read /foo/bar.py]"
    assert _format_tool_use({"name": "Bash", "input": {"command": "ls -la"}}) == "[Bash: ls -la]"
    assert _format_tool_use({"name": "Grep", "input": {"pattern": "TODO"}}) == "[Grep: TODO]"
    assert _format_tool_use({"name": "Agent", "input": {"description": "research task"}}) == "[Agent: research task]"
    assert _format_tool_use({"name": "Skill", "input": {}}) == "[Skill]"


def test_format_bash_result_error():
    result = _format_bash_result("error line 1\nerror line 2", is_error=True)
    assert "error line 1" in result
    assert "error line 2" in result


def test_format_bash_result_short():
    result = _format_bash_result("ok", is_error=False)
    assert result == "ok"


def test_format_bash_result_long():
    lines = "\n".join(f"line {i}" for i in range(20))
    result = _format_bash_result(lines, is_error=False)
    assert "line 0" in result
    assert "line 1" in result
    assert "..." in result
    assert "line 19" in result


def test_extract_user_text_string():
    assert _extract_user_text("hello") == "hello"


def test_extract_user_text_array():
    content = [{"type": "text", "text": "hello"}, {"type": "tool_result", "content": "ignored"}]
    assert _extract_user_text(content) == "hello"


def test_parse_empty_file(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    session = parse_jsonl(str(empty))
    assert session.session_id == ""
    assert session.user_message_count == 0
