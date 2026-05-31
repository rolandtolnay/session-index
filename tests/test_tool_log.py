"""Tests for per-session tool log writer."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import ParsedToolCall
from tool_log import extract_tool_log_section, write_tool_log


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


def test_extract_tool_log_section_returns_exact_section_with_locator(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    calls = [
        ParsedToolCall(sequence=1, scope="main", tool_name="Read", result="first"),
        ParsedToolCall(sequence=3, scope="main", tool_name="Edit", arguments={"path": "src/app.py"}, result="changed"),
        ParsedToolCall(sequence=1000, scope="main", tool_name="Bash", result="last"),
    ]
    path = write_tool_log("session-1", calls)

    section = extract_tool_log_section(path, 3)

    assert section is not None
    assert section.path == path
    assert section.sequence == 3
    assert section.heading.startswith("## 003 — main — Edit")
    assert section.line_start is not None
    assert section.line_end is not None
    assert section.text.startswith("## 003 — main — Edit")
    assert '"path": "src/app.py"' in section.text
    assert "changed" in section.text
    assert "## 1000" not in section.text
    assert "last" not in section.text


def test_extract_tool_log_section_ignores_heading_like_result_text(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    calls = [
        ParsedToolCall(sequence=1, tool_name="Bash", result="before\n## 002 — fake — heading — 10:00:00\nafter"),
        ParsedToolCall(sequence=2, tool_name="Read", result="real second"),
    ]
    path = write_tool_log("session-1", calls)

    section = extract_tool_log_section(path, 2)

    assert section is not None
    assert section.heading.startswith("## 002 — main — Read")
    assert "real second" in section.text
    assert "fake" not in section.text


def test_extract_tool_log_section_supports_unpadded_large_sequences(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    path = write_tool_log("session-1", [ParsedToolCall(sequence=1000, tool_name="Bash", result="big")])

    section = extract_tool_log_section(path, 1000)

    assert section is not None
    assert section.heading.startswith("## 1000 — main — Bash")
    assert "big" in section.text


def test_extract_tool_log_section_missing_file_or_sequence_returns_none(tmp_path):
    assert extract_tool_log_section(str(tmp_path / "missing.tools.md"), 1) is None
    path = tmp_path / "session.tools.md"
    path.write_text("# Tool log\n\n## 001 — main — Read — unknown\nbody\n")
    assert extract_tool_log_section(str(path), 2) is None
