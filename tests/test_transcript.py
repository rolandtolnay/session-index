"""Tests for the transcript writer."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transcript import write_transcript, write_subagent_transcript, SubagentRef, extract_excerpts, _block_role


def test_write_transcript(tmp_path, monkeypatch):
    # Redirect transcript dir to tmp
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))

    messages = [
        {"role": "user", "content": "Fix the bug"},
        {"role": "assistant", "content": "I found the issue in the login handler."},
        {"role": "user", "content": "Looks good"},
        {"role": "assistant", "content": "Done!"},
    ]

    path = write_transcript(
        "test-session",
        messages,
        project="myproject",
        branch="main",
        timestamp="2026-04-01T14:23:45Z",
    )

    assert os.path.exists(path)
    content = open(path).read()
    # Header
    assert "myproject" in content
    assert "main" in content
    assert "2026-04-01" in content
    assert "---" in content
    # Messages with fenced delimiters
    assert "[user]" in content
    assert "Fix the bug" in content
    assert "[assistant]" in content
    assert "I found the issue" in content
    assert "Looks good" in content
    assert "Done!" in content


def test_write_transcript_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    path = write_transcript("empty-session", [])
    assert os.path.exists(path)
    content = open(path).read()
    # Header still present even with no messages
    assert "---" in content


# ── Subagent marker expansion in parent transcript ─────────────────────────


def test_subagent_markers_expanded_with_refs(tmp_path, monkeypatch):
    """Subagent markers should expand to 3-line blocks with → see: links."""
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    messages = [
        {"role": "user", "content": "Do something", "timestamp": "2026-01-15T10:00:00.000Z"},
        {"role": "assistant", "content": "I'll delegate.\n__SUBAGENT:Explore:find config files__\nDone.",
         "timestamp": "2026-01-15T10:00:01.000Z"},
    ]
    refs = [SubagentRef(agent_type="Explore", agent_id="abc123")]
    path = write_transcript("test-session", messages, subagents=refs)
    content = open(path).read()
    assert "[subagent] Explore ─" in content
    assert "find config files" in content
    assert "→ see: agent-abc123.md" in content
    # Raw marker should not appear
    assert "__SUBAGENT:" not in content


def test_subagent_markers_without_refs(tmp_path, monkeypatch):
    """Without subagent refs, markers expand but have no → see: link."""
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    messages = [
        {"role": "assistant", "content": "__SUBAGENT:Explore:find stuff__",
         "timestamp": "2026-01-15T10:00:01.000Z"},
    ]
    path = write_transcript("test-session", messages)
    content = open(path).read()
    # Without refs, the raw marker stays (no expansion)
    assert "__SUBAGENT:" in content


def test_subagent_multiple_markers_matched_in_order(tmp_path, monkeypatch):
    """Multiple markers should match to subagent refs in order."""
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    messages = [
        {"role": "assistant",
         "content": "__SUBAGENT:Explore:first task__\nSome text\n__SUBAGENT:Plan:second task__",
         "timestamp": "2026-01-15T10:00:01.000Z"},
    ]
    refs = [
        SubagentRef(agent_type="Explore", agent_id="aaa111"),
        SubagentRef(agent_type="Plan", agent_id="bbb222"),
    ]
    path = write_transcript("test-session", messages, subagents=refs)
    content = open(path).read()
    assert "→ see: agent-aaa111.md" in content
    assert "→ see: agent-bbb222.md" in content
    # Verify order: first ref appears before second
    assert content.index("agent-aaa111") < content.index("agent-bbb222")


def test_subagent_markers_more_markers_than_refs(tmp_path, monkeypatch):
    """Extra markers beyond available refs should expand without → see: link."""
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    messages = [
        {"role": "assistant",
         "content": "__SUBAGENT:Explore:first__\n__SUBAGENT:agent:second__",
         "timestamp": ""},
    ]
    refs = [SubagentRef(agent_type="Explore", agent_id="only1")]
    path = write_transcript("test-session", messages, subagents=refs)
    content = open(path).read()
    assert "→ see: agent-only1.md" in content
    # Second marker expands visually but has no link
    assert "[subagent] agent ─" in content
    assert content.count("→ see:") == 1


def test_subagent_consecutive_no_extra_blank_lines(tmp_path, monkeypatch):
    """Consecutive subagent blocks should not have blank lines between them."""
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    messages = [
        {"role": "assistant",
         "content": "Let me explore.\n__SUBAGENT:Explore:first__\n__SUBAGENT:Explore:second__\n__SUBAGENT:Explore:third__\nDone.",
         "timestamp": "2026-01-15T10:00:01.000Z"},
    ]
    refs = [
        SubagentRef(agent_type="Explore", agent_id="a1"),
        SubagentRef(agent_type="Explore", agent_id="a2"),
        SubagentRef(agent_type="Explore", agent_id="a3"),
    ]
    path = write_transcript("test-session", messages, subagents=refs)
    content = open(path).read()
    # All three should be present
    assert "→ see: agent-a1.md" in content
    assert "→ see: agent-a2.md" in content
    assert "→ see: agent-a3.md" in content
    # Between consecutive subagents, no blank lines (only the first gets one)
    subagent_section = content.split("→ see: agent-a1.md")[1].split("Done.")[0]
    assert "\n\n[subagent]" not in subagent_section


def test_subagent_blank_line_before_first_in_run(tmp_path, monkeypatch):
    """A blank line should separate assistant text from the first subagent block."""
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    messages = [
        {"role": "assistant",
         "content": "Some text.\n__SUBAGENT:Explore:task__",
         "timestamp": "2026-01-15T10:00:01.000Z"},
    ]
    refs = [SubagentRef(agent_type="Explore", agent_id="x1")]
    path = write_transcript("test-session", messages, subagents=refs)
    content = open(path).read()
    assert "Some text.\n[subagent]" in content


def test_subagent_new_run_after_text_gets_blank_line(tmp_path, monkeypatch):
    """Text between subagent runs should have blank lines around each run."""
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    messages = [
        {"role": "assistant",
         "content": "__SUBAGENT:Explore:first__\nMiddle text.\n__SUBAGENT:Plan:second__",
         "timestamp": ""},
    ]
    refs = [
        SubagentRef(agent_type="Explore", agent_id="r1"),
        SubagentRef(agent_type="Plan", agent_id="r2"),
    ]
    path = write_transcript("test-session", messages, subagents=refs)
    content = open(path).read()
    # Blank line before second run (after "Middle text.")
    assert "Middle text.\n[subagent] Plan" in content


# ── Subagent transcript tests ──────────────────────────────────────────────


def test_write_subagent_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    from subagent_parser import ParsedSubagent

    parsed = ParsedSubagent(
        agent_id="abc123",
        agent_type="Explore",
        parent_session_id="parent-session",
        started_at="2026-04-01T14:00:00.000Z",
        ended_at="2026-04-01T14:05:00.000Z",
        duration_seconds=300,
        files_touched=["/src/main.py"],
        tools_used="Read:2, Bash:1",
        tool_call_count=3,
        messages=[
            {"role": "prompt", "content": "Find the entry point", "timestamp": "2026-04-01T14:00:00.000Z"},
            {"role": "agent", "content": "Let me look at the project.\n\u2192 Bash: ls -la", "timestamp": "2026-04-01T14:00:02.000Z"},
            {"role": "error", "content": "command not found: foobar", "timestamp": "2026-04-01T14:00:03.000Z"},
            {"role": "agent", "content": "The entry point is main.py.", "timestamp": "2026-04-01T14:00:05.000Z"},
        ],
        initial_prompt="Find the entry point",
    )

    path = write_subagent_transcript("parent-session", parsed)
    assert os.path.exists(path)
    assert "parent-session" in path
    assert "agent-abc123.md" in path

    content = open(path).read()
    # Header
    assert "Explore" in content
    assert "Parent: parent-session" in content
    assert "5 min" in content
    assert "3 tool calls" in content
    assert "1 files" in content
    # Role labels
    assert "[prompt]" in content
    assert "[agent]" in content
    assert "[error]" in content
    # Content
    assert "Find the entry point" in content
    assert "\u2192 Bash: ls -la" in content
    assert "command not found" in content
    assert "main.py" in content


def test_subagent_transcript_creates_subdir(tmp_path, monkeypatch):
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    from subagent_parser import ParsedSubagent

    parsed = ParsedSubagent(
        agent_id="xyz789",
        agent_type="Explore",
        messages=[
            {"role": "prompt", "content": "hello", "timestamp": ""},
        ],
    )

    path = write_subagent_transcript("my-session-id", parsed)
    subdir = os.path.join(str(tmp_path), "my-session-id")
    assert os.path.isdir(subdir)
    assert os.path.exists(path)


# ── Excerpt extraction tests ────────────────────────────────────────────────


def _write_test_transcript(tmp_path, messages):
    """Helper: write a transcript and return its path."""
    path = str(tmp_path / "test.md")
    lines = ["test | project | 2026-04-01", "---", ""]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "user":
            lines.append(f"[user] {'─' * 40}")
        else:
            lines.append(f"[assistant] {'─' * 34}")
        lines.append(content)
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def test_excerpt_matches_keyword(tmp_path):
    path = _write_test_transcript(tmp_path, [
        {"role": "user", "content": "Fix the authentication timeout bug"},
        {"role": "assistant", "content": "I found the issue in the auth module."},
        {"role": "user", "content": "Add pagination to the API"},
        {"role": "assistant", "content": "Done with pagination."},
    ])
    result = extract_excerpts(path, ["authentication"])
    assert result is not None
    assert "authentication timeout" in result
    assert "pagination" not in result


def test_excerpt_returns_full_block(tmp_path):
    path = _write_test_transcript(tmp_path, [
        {"role": "user", "content": "Fix the auth bug"},
        {"role": "assistant", "content": "The auth module had a token expiry issue.\nI updated the TTL to 1 hour."},
    ])
    result = extract_excerpts(path, ["auth"])
    assert result is not None
    # Both blocks should match (both contain "auth")
    assert "Fix the auth bug" in result
    assert "token expiry issue" in result
    assert "TTL to 1 hour" in result


def test_excerpt_caps_at_max_blocks(tmp_path):
    messages = []
    for i in range(10):
        messages.append({"role": "user", "content": f"auth question {i}"})
        messages.append({"role": "assistant", "content": f"auth answer {i}"})
    path = _write_test_transcript(tmp_path, messages)
    result = extract_excerpts(path, ["auth"], max_blocks=3)
    assert result is not None
    # Hybrid strategy prefers later blocks; Q/A pairing pulls in partners
    # Should have later content (not all 20 blocks) + truncation note
    assert "auth question 9" in result or "auth answer 9" in result
    assert "not shown" in result


def test_excerpt_first_n_strategy(tmp_path):
    """First-N strategy returns earliest matching blocks."""
    from transcript import STRATEGY_FIRST_N
    messages = [
        {"role": "user", "content": "auth setup early"},
        {"role": "assistant", "content": "auth early answer"},
        {"role": "user", "content": "unrelated middle topic"},
        {"role": "assistant", "content": "middle answer"},
        {"role": "user", "content": "auth decision final"},
        {"role": "assistant", "content": "auth final implementation"},
    ]
    path = _write_test_transcript(tmp_path, messages)
    result = extract_excerpts(path, ["auth"], max_blocks=2, strategy=STRATEGY_FIRST_N, qa_pair=False)
    assert "auth setup early" in result
    assert "auth decision final" not in result


def test_excerpt_hybrid_prefers_later(tmp_path):
    """Hybrid strategy prefers later blocks with same keyword density."""
    from transcript import STRATEGY_HYBRID
    messages = [
        {"role": "user", "content": "auth setup early"},
        {"role": "assistant", "content": "exploring auth options"},
        {"role": "user", "content": "auth decision final"},
        {"role": "assistant", "content": "auth final implementation done"},
    ]
    path = _write_test_transcript(tmp_path, messages)
    result = extract_excerpts(path, ["auth"], max_blocks=2, strategy=STRATEGY_HYBRID, qa_pair=False)
    # Hybrid should pick the later blocks (higher recency weight)
    assert "auth decision final" in result or "auth final implementation" in result


def test_excerpt_qa_pairing(tmp_path):
    """Q/A pairing pulls in adjacent partner blocks."""
    messages = [
        {"role": "user", "content": "unrelated question"},
        {"role": "assistant", "content": "unrelated answer"},
        {"role": "user", "content": "what about the auth flow?"},
        {"role": "assistant", "content": "the auth flow uses JWT tokens with refresh"},
    ]
    path = _write_test_transcript(tmp_path, messages)
    # Without pairing: only the assistant block (higher density) matches
    from transcript import STRATEGY_DENSITY
    result_no_qa = extract_excerpts(path, ["auth"], max_blocks=1, strategy=STRATEGY_DENSITY, qa_pair=False)
    assert "JWT tokens" in result_no_qa
    assert "what about" not in result_no_qa
    # With pairing: user question gets pulled in
    result_qa = extract_excerpts(path, ["auth"], max_blocks=1, strategy=STRATEGY_DENSITY, qa_pair=True)
    assert "JWT tokens" in result_qa
    assert "what about the auth flow" in result_qa


def test_excerpt_no_match_returns_none(tmp_path):
    path = _write_test_transcript(tmp_path, [
        {"role": "user", "content": "Fix the login bug"},
        {"role": "assistant", "content": "Done."},
    ])
    result = extract_excerpts(path, ["pagination"])
    assert result is None


def test_excerpt_missing_file_returns_none():
    result = extract_excerpts("/nonexistent/path.md", ["auth"])
    assert result is None


def test_excerpt_skips_short_keywords(tmp_path):
    path = _write_test_transcript(tmp_path, [
        {"role": "user", "content": "Fix the auth bug"},
    ])
    # Keywords with <= 2 chars should be skipped
    result = extract_excerpts(path, ["an", "to"])
    assert result is None


def _write_agent_transcript(tmp_path, messages, filename="agent-test.md"):
    """Helper: write an agent-style transcript using [prompt]/[agent] markers."""
    path = str(tmp_path / filename)
    lines = ["# general-purpose — 2026-04-01 14:40", "Parent: test-session", "---", ""]
    for msg in messages:
        role = msg["role"]
        lines.append(f"[{role}] 14:40 {'─' * 30}")
        lines.append(msg["content"])
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def test_block_role_maps_prompt_and_agent():
    # Subagent transcripts use [prompt]/[agent]; treat them as user/assistant so
    # downstream Q/A pairing works uniformly.
    assert _block_role("[prompt] 14:40 ──────────────────────────────") == "user"
    assert _block_role("[agent] 14:40 ──────────────────────────────") == "assistant"
    assert _block_role("[user] 14:40 ──────────────────────────────") == "user"
    assert _block_role("[assistant] 14:40 ──────────────────────────────") == "assistant"
    assert _block_role("header line with no role") is None


def test_excerpt_parses_agent_transcript(tmp_path):
    # Regression: before [prompt]/[agent] were added to _ROLE_RE, agent transcripts
    # parsed as a single header block and extract_excerpts returned None.
    path = _write_agent_transcript(tmp_path, [
        {"role": "prompt", "content": "Investigate claude-mem and episodic-memory trade-offs"},
        {"role": "agent", "content": "I found that claude-mem has a ~72% summary failure rate."},
    ])
    result = extract_excerpts(path, ["claude-mem"])
    assert result is not None
    assert "claude-mem" in result
