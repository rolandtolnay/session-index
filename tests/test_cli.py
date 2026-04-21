"""Tests for CLI helpers."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import _print_agent_excerpts


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
