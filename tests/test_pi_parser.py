"""Tests for Pi JSONL parser."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pi_parser import parse_pi_jsonl

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pi_sample.jsonl")
QUESTION_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pi_question.jsonl")


def test_parse_pi_metadata(monkeypatch):
    monkeypatch.setattr("pi_parser._git_branch", lambda cwd: "main")
    session = parse_pi_jsonl(FIXTURE)

    assert session.session_id == "pi:019pi-sample-0001"
    assert session.project == "project"
    assert session.branch == "main"
    assert session.model == "gpt-5.5"
    assert session.started_at == "2026-04-01T10:00:00.000Z"
    assert session.ended_at == "2026-04-01T10:00:12.000Z"
    assert session.duration_seconds == 12
    assert session.parent_session_path == "2026-04-01T09-30-00-000Z_019ddfb1-7362-7526-8b21-8a6d77c82fe0.jsonl"
    assert session.parent_native_session_id == "019ddfb1-7362-7526-8b21-8a6d77c82fe0"


def test_parse_pi_active_branch_only():
    session = parse_pi_jsonl(FIXTURE)

    assert session.user_message_count == 2
    assert session.user_messages == [
        "Fix the Pi parser in app.py",
        "Use a safer active-branch parser instead",
    ]

    all_content = "\n".join(m["content"] for m in session.messages)
    assert "Abandoned branch tried a risky parser rewrite" in all_content
    assert "Try the abandoned approach" not in all_content
    assert "This abandoned branch should not be indexed" not in all_content


def test_parse_pi_tools_and_files():
    session = parse_pi_jsonl(FIXTURE)

    assert "/Users/test/project/app.py" in session.files_touched
    assert "read:1" in session.tools_used
    assert "edit:1" in session.tools_used


def test_parse_pi_tool_calls_active_branch_only():
    session = parse_pi_jsonl(FIXTURE)

    assert [call.tool_call_id for call in session.tool_calls] == ["call-read", "call-edit"]
    read_call = session.tool_calls[0]
    assert read_call.tool_name == "read"
    assert read_call.arguments["path"] == "/Users/test/project/app.py"
    assert read_call.result == "<file content omitted>"
    assert read_call.is_error is False


def test_parse_pi_messages_skip_thinking_and_tool_results():
    session = parse_pi_jsonl(FIXTURE)
    all_content = "\n".join(m["content"] for m in session.messages)

    assert "Need to edit carefully" not in all_content
    assert "<file content omitted>" not in all_content
    assert "Applied the safer parser change" in all_content
    assert "Done." in all_content


def test_parse_pi_captures_question_details_and_synthesizes_result(monkeypatch):
    monkeypatch.setattr("pi_parser._git_branch", lambda cwd: "main")
    session = parse_pi_jsonl(QUESTION_FIXTURE)

    q_calls = [c for c in session.tool_calls if c.tool_name == "question"]
    assert len(q_calls) == 1
    call = q_calls[0]

    # `details` preserved as the authoritative structured form.
    assert call.result_details.get("selections")
    assert call.result_details["selections"][0]["answer"] == "Future + existing"

    # Empty standard `content` is synthesized into a readable answer string,
    # so the tool log no longer shows [empty result] for an answered question.
    assert "User answered questions:" in call.result
    assert "Future + existing" in call.result
