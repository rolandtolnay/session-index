"""Tests for Codex rollout JSONL parser."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_parser import parse_codex_jsonl

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "codex_sample.jsonl")


def test_parse_codex_metadata(monkeypatch):
    monkeypatch.setenv("SESSION_INDEX_CODEX_HOME", "/tmp/no-codex-home")
    session = parse_codex_jsonl(FIXTURE)

    assert session.session_id == "codex:019codex-0000-7000-8000-000000000001"
    assert session.project == "project"
    assert session.branch == "main"
    assert session.model == "gpt-5.5"
    assert session.started_at == "2026-06-24T10:00:00.000Z"
    assert session.ended_at == "2026-06-24T10:00:08.010Z"
    assert session.duration_seconds == 8


def test_parse_codex_visible_messages_only(monkeypatch):
    monkeypatch.setenv("SESSION_INDEX_CODEX_HOME", "/tmp/no-codex-home")
    session = parse_codex_jsonl(FIXTURE)

    assert session.user_messages == ["Fix the Codex parser in app.py"]
    assert session.user_message_count == 1
    assert session.assistant_message_count == 2

    all_content = "\n".join(m["content"] for m in session.messages)
    assert "Fix the Codex parser in app.py" in all_content
    assert "I am checking the parser shape." in all_content
    assert "Done. The parser handles Codex rollouts." in all_content
    assert "developer instructions should not be indexed" not in all_content
    assert "AGENTS.md instructions should not be indexed" not in all_content


def test_parse_codex_tools_patch_files_and_subagent_request(monkeypatch):
    monkeypatch.setenv("SESSION_INDEX_CODEX_HOME", "/tmp/no-codex-home")
    session = parse_codex_jsonl(FIXTURE)

    assert "/Users/test/project/app.py" in session.files_touched
    assert "exec_command:1" in session.tools_used
    assert "apply_patch:1" in session.tools_used
    assert "spawn_agent:1" in session.tools_used

    names = [call.tool_name for call in session.tool_calls]
    assert names == ["exec_command", "apply_patch", "spawn_agent"]

    read_call = session.tool_calls[0]
    assert read_call.tool_call_id == "call-read"
    assert read_call.arguments["cmd"] == "sed -n '1,80p' app.py"
    assert "print('hello')" in read_call.result
    assert read_call.is_error is False

    patch_call = session.tool_calls[1]
    assert patch_call.tool_name == "apply_patch"
    assert patch_call.arguments["changes"][0]["path"] == "/Users/test/project/app.py"
    assert patch_call.is_error is False
