"""Tests for per-session tool log writer."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import ParsedToolCall
from tool_log import compact_tool_log_file, extract_tool_log_section, write_tool_log


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


def test_write_tool_log_compacts_huge_read_only_result(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    result = "a" * 5_000 + "middle" + "z" * 5_000
    calls = [ParsedToolCall(
        sequence=1,
        tool_name="Read",
        arguments={"file_path": "large.md"},
        result=result,
    )]

    path = write_tool_log("session-1", calls)
    content = open(path).read()

    assert "[compact read-only result: showing first 500 and last 500 of 10006 characters; 9006 omitted]" in content
    assert '"file_path": "large.md"' in content
    assert "middle" not in content
    assert len(content) < 2_000


def test_write_tool_log_compacts_read_only_shell_command_result(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    result = "line\n" * 3_000
    calls = [ParsedToolCall(
        sequence=1,
        tool_name="functions.exec_command",
        arguments={"cmd": "sed -n '1,240p' indexer.py"},
        result=result,
    )]

    path = write_tool_log("session-1", calls)
    content = open(path).read()

    assert "[compact read-only result:" in content
    assert len(content) < 2_500


def test_write_tool_log_keeps_mutation_result_on_larger_audit_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    result = "a" * 10_500 + "middle" + "z" * 10_500
    calls = [ParsedToolCall(
        sequence=1,
        tool_name="Edit",
        arguments={"file_path": "app.py", "old_string": "a", "new_string": "b"},
        result=result,
    )]

    path = write_tool_log("session-1", calls)
    content = open(path).read()

    assert "[compact read-only result:" not in content
    assert "[truncated: showing first 10000 and last 10000 of 21006 characters]" in content
    assert '"file_path": "app.py"' in content


def test_write_tool_log_compacts_large_write_content_argument(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    body = "a" * 5_000 + "middle" + "z" * 5_000
    calls = [ParsedToolCall(
        sequence=1,
        tool_name="Write",
        arguments={"file_path": "large.html", "content": body},
        result="wrote file",
    )]

    path = write_tool_log("session-1", calls)
    content = open(path).read()

    assert '"file_path": "large.html"' in content
    assert '"__session_index_compacted_text__": true' in content
    assert '"chars": 10006' in content
    assert '"sha256":' in content
    assert '"omitted": 7006' in content
    assert "middle" not in content
    assert "wrote file" in content
    assert len(content) < 4_500


def test_write_tool_log_compacts_large_edit_strings_but_keeps_path(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    old = "old-" * 2_000
    new = "new-" * 2_000
    calls = [ParsedToolCall(
        sequence=1,
        tool_name="Edit",
        arguments={"file_path": "app.py", "old_string": old, "new_string": new},
        result="changed",
    )]

    path = write_tool_log("session-1", calls)
    content = open(path).read()

    assert '"file_path": "app.py"' in content
    assert content.count('"__session_index_compacted_text__": true') == 2
    assert "old-old-old" in content
    assert "new-new-new" in content
    assert "changed" in content


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


def test_compact_tool_log_file_migrates_existing_large_arguments_and_read_results(tmp_path):
    path = tmp_path / "old.tools.md"
    body = "a" * 5_000 + "middle" + "z" * 5_000
    read_result = "r" * 3_000 + "middle-read" + "s" * 3_000
    path.write_text(
        "# Tool log — old\n\n---\n\n"
        "## 001 — main — Write — unknown\n\n"
        "Status: ok\n"
        "Tool call ID: tool-1\n\n"
        "Arguments:\n```json\n"
        "{\n"
        "  \"content\": " + json.dumps(body) + ",\n"
        "  \"file_path\": \"large.md\"\n"
        "}\n"
        "```\n\n"
        "Result:\n```text\nok\n```\n\n"
        "## 002 — main — Read — unknown\n\n"
        "Status: ok\n"
        "Tool call ID: tool-2\n\n"
        "Arguments:\n```json\n{\"file_path\": \"large.md\"}\n```\n\n"
        "Result:\n```text\n" + read_result + "\n```\n"
    )

    result = compact_tool_log_file(str(path))
    content = path.read_text()

    assert result is not None
    assert result.changed is True
    assert result.compacted_bytes < result.original_bytes
    assert '"file_path": "large.md"' in content
    assert '"__session_index_compacted_text__": true' in content
    assert "[compact read-only result: showing first 500 and last 500" in content
    assert "middle" not in content
    assert "middle-read" not in content


def test_compact_tool_log_file_is_idempotent_for_already_compacted_result(tmp_path):
    path = tmp_path / "old.tools.md"
    path.write_text(
        "# Tool log — old\n\n---\n\n"
        "## 001 — main — Read — unknown\n\n"
        "Status: ok\n"
        "Tool call ID: tool-1\n\n"
        "Arguments:\n```json\n{\"file_path\": \"large.md\"}\n```\n\n"
        "Result:\n```text\n"
        "aaa\n\n[compact read-only result: showing first 1000 and last 1000 of 3000 characters; 1000 omitted]\n\nzzz\n"
        "```\n"
    )

    first = compact_tool_log_file(str(path))
    result = compact_tool_log_file(str(path))

    assert first is not None
    assert result is not None
    assert result.changed is False
