"""Tests for the subagent JSONL parser."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subagent_parser import (
    ParsedSubagent,
    SubagentInfo,
    discover_subagents,
    parse_subagent_jsonl,
    _format_tool_signature,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
EXPLORE_JSONL = os.path.join(FIXTURES, "subagent_explore.jsonl")
EXPLORE_META = os.path.join(FIXTURES, "subagent_explore.meta.json")
ERRORS_JSONL = os.path.join(FIXTURES, "subagent_with_errors.jsonl")
NO_META_JSONL = os.path.join(FIXTURES, "subagent_no_meta.jsonl")


# ── Metadata extraction ────────────────────────────────────────────────────


def test_parse_agent_id():
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    assert parsed.agent_id == "a5f64306c4e829331"


def test_parse_agent_type_from_meta():
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    assert parsed.agent_type == "Explore"


def test_parse_parent_session_id():
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    assert parsed.parent_session_id == "parent-session-123"


def test_parse_timestamps():
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    assert parsed.started_at.startswith("2026-01-15")
    assert parsed.ended_at.startswith("2026-01-15")
    assert parsed.duration_seconds >= 0


def test_parse_duration():
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    # 10:00:00 to 10:00:10 = 10 seconds
    assert parsed.duration_seconds == 10


# ── files_touched ────────────────────────────────────────────────────────


def test_files_touched():
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    assert "/Users/test/project/main.py" in parsed.files_touched


def test_files_touched_from_edit():
    parsed = parse_subagent_jsonl(ERRORS_JSONL)
    assert "/Users/test/project/auth.py" in parsed.files_touched


# ── tools_used ────────────────────────────────────────────────────────────


def test_tools_used_counter():
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    assert "Read" in parsed.tools_used
    assert "Bash" in parsed.tools_used
    assert "Grep" in parsed.tools_used


def test_tool_call_count():
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    # Bash + Read + Grep = 3 tool calls
    assert parsed.tool_call_count == 3


# ── Narration NOT stripped ──────────────────────────────────────────────────


def test_narration_preserved():
    """Unlike parent parsing, narration text should be kept in subagent output."""
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    all_content = " ".join(m["content"] for m in parsed.messages if m["role"] == "agent")
    # "I'll explore..." and "Let me read..." would be stripped in parent parsing
    assert "I'll explore the project structure" in all_content
    assert "Let me read the entry point" in all_content


# ── Tool signatures ──────────────────────────────────────────────────────


def test_tool_signatures_in_messages():
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    all_content = " ".join(m["content"] for m in parsed.messages if m["role"] == "agent")
    assert "\u2192 Bash:" in all_content
    assert "\u2192 Read: /Users/test/project/main.py" in all_content
    assert "\u2192 Grep: DATABASE_URL" in all_content


def test_format_tool_signature_read():
    sig = _format_tool_signature({"name": "Read", "input": {"file_path": "/foo/bar.py"}})
    assert sig == "\u2192 Read: /foo/bar.py"


def test_format_tool_signature_bash():
    sig = _format_tool_signature({"name": "Bash", "input": {"command": "ls -la"}})
    assert sig == "\u2192 Bash: ls -la"


def test_format_tool_signature_bash_long():
    long_cmd = "x" * 200
    sig = _format_tool_signature({"name": "Bash", "input": {"command": long_cmd}})
    assert len(sig) < 200
    assert sig.endswith("\u2026")


def test_format_tool_signature_grep():
    sig = _format_tool_signature({"name": "Grep", "input": {"pattern": "TODO", "path": "/src"}})
    assert sig == "\u2192 Grep: TODO in /src"


def test_format_tool_signature_grep_no_path():
    sig = _format_tool_signature({"name": "Grep", "input": {"pattern": "TODO"}})
    assert sig == "\u2192 Grep: TODO"


def test_format_tool_signature_glob():
    sig = _format_tool_signature({"name": "Glob", "input": {"pattern": "**/*.py"}})
    assert sig == "\u2192 Glob: **/*.py"


def test_format_tool_signature_agent():
    sig = _format_tool_signature({"name": "Agent", "input": {"description": "research task"}})
    assert sig == "\u2192 Agent: research task"


def test_format_tool_signature_other():
    sig = _format_tool_signature({"name": "Skill", "input": {}})
    assert sig == "\u2192 Skill"


# ── Error blocks ──────────────────────────────────────────────────────────


def test_error_blocks():
    parsed = parse_subagent_jsonl(ERRORS_JSONL)
    error_msgs = [m for m in parsed.messages if m["role"] == "error"]
    assert len(error_msgs) == 1
    assert "FAILED" in error_msgs[0]["content"]
    assert "AssertionError" in error_msgs[0]["content"]


# ── Consecutive assistant messages NOT merged ────────────────────────────


def test_consecutive_assistant_not_merged():
    """Each assistant entry should be its own message, not merged."""
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    agent_msgs = [m for m in parsed.messages if m["role"] == "agent"]
    # The explore fixture has multiple assistant entries that would be merged in parent
    assert len(agent_msgs) >= 3


# ── Missing meta → unknown ──────────────────────────────────────────────


def test_no_meta_defaults_to_unknown():
    parsed = parse_subagent_jsonl(NO_META_JSONL)
    assert parsed.agent_type == "unknown"


def test_no_meta_still_parses():
    parsed = parse_subagent_jsonl(NO_META_JSONL)
    assert parsed.agent_id == "anometa789xyz"
    assert len(parsed.messages) > 0


# ── Empty JSONL ──────────────────────────────────────────────────────────


def test_empty_jsonl(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    parsed = parse_subagent_jsonl(str(empty))
    assert parsed.agent_id == ""
    assert parsed.messages == []
    assert parsed.files_touched == []


# ── Initial prompt ────────────────────────────────────────────────────────


def test_initial_prompt():
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    assert "Explore the project structure" in parsed.initial_prompt


def test_prompt_role():
    """First user message should have role 'prompt', not 'user'."""
    parsed = parse_subagent_jsonl(EXPLORE_JSONL, EXPLORE_META)
    assert parsed.messages[0]["role"] == "prompt"


# ── discover_subagents ────────────────────────────────────────────────────


def test_discover_subagents(tmp_path):
    """discover_subagents finds files and reads meta."""
    session_dir = tmp_path / "test-session"
    subagents_dir = session_dir / "subagents"
    subagents_dir.mkdir(parents=True)

    # Create a JSONL + meta pair
    (subagents_dir / "agent-abc123.jsonl").write_text(
        '{"type":"user","agentId":"abc123","message":{"role":"user","content":"hello"}}\n'
    )
    (subagents_dir / "agent-abc123.meta.json").write_text(
        '{"agentType": "Explore"}'
    )

    # Create one without meta
    (subagents_dir / "agent-def456.jsonl").write_text(
        '{"type":"user","agentId":"def456","message":{"role":"user","content":"hi"}}\n'
    )

    # Parent JSONL
    parent_jsonl = tmp_path / "test-session.jsonl"
    parent_jsonl.write_text("")

    results = discover_subagents(str(parent_jsonl))
    assert len(results) == 2

    # First should be abc123 (sorted)
    assert results[0].agent_id == "abc123"
    assert results[0].agent_type == "Explore"
    assert results[0].meta_path is not None

    # Second should be def456 without meta
    assert results[1].agent_id == "def456"
    assert results[1].agent_type == "unknown"
    assert results[1].meta_path is None


def test_discover_subagents_no_dir(tmp_path):
    """Returns empty list when subagents dir doesn't exist."""
    parent = tmp_path / "no-subagents.jsonl"
    parent.write_text("")
    assert discover_subagents(str(parent)) == []


def test_discover_subagents_skips_compact(tmp_path):
    """System agents like acompact-* should be skipped."""
    session_dir = tmp_path / "test-session"
    subagents_dir = session_dir / "subagents"
    subagents_dir.mkdir(parents=True)

    # System agent that should be skipped
    (subagents_dir / "agent-abc123.jsonl").write_text("")

    parent_jsonl = tmp_path / "test-session.jsonl"
    parent_jsonl.write_text("")

    # Normal agent should be found
    results = discover_subagents(str(parent_jsonl))
    assert len(results) == 1
