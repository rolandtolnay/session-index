"""Follow generated artifact paths, rather than asserting navigation copy."""

import json
import re
import shutil
from pathlib import Path

import pytest

import db
import indexer
import tool_log
import transcript


@pytest.fixture
def storage(tmp_path, monkeypatch):
    data = tmp_path / "data with spaces"
    artifacts = data / "transcripts"
    monkeypatch.setattr(db, "DATA_DIR", str(data))
    monkeypatch.setattr(db, "DB_PATH", str(data / "sessions.db"))
    monkeypatch.setattr(transcript, "TRANSCRIPT_DIR", str(artifacts))
    monkeypatch.setattr(tool_log, "TRANSCRIPT_DIR", str(artifacts))
    return artifacts


def navigation_paths(text):
    header = text.split("[user]", 1)[0]
    return [Path(value) for value in re.findall(r"`([^`]+)`", header) if Path(value).is_absolute()]


@pytest.mark.parametrize("source,fixture", [
    ("claude", "sample.jsonl"),
    ("pi", "pi_sample.jsonl"),
    ("codex", "codex_sample.jsonl"),
])
def test_indexed_clean_transcript_links_to_generated_tool_log(storage, tmp_path, source, fixture):
    source_path = tmp_path / fixture
    shutil.copyfile(Path(__file__).parent / "fixtures" / fixture, source_path)
    result = indexer.index_source_transcript(source, str(source_path), indexer.NO_SUMMARY_INDEX_OPTIONS)
    assert not result.skipped_reason
    text = Path(result.transcript_path).read_text()
    links = navigation_paths(text)
    tool_path = next(path for path in links if path.suffix == ".md")
    assert tool_path == Path(result.tool_log_path)
    assert tool_path.is_file()
    tool_line = next(line for line in text.splitlines() if str(tool_path) in line)
    assert "(available)" in tool_line  # Siblings must be written before this snapshot.
    assert not any(path.is_dir() for path in links)


def test_background_output_is_reachable_from_clean_transcript(storage, tmp_path):
    parent = tmp_path / "parent.jsonl"
    entries = [
        {"type": "session", "id": "navigation-parent", "cwd": str(tmp_path)},
        {"type": "message", "id": "u", "parentId": None, "message": {
            "role": "user", "content": [{"type": "text", "text": "Review the change"}],
        }},
        {"type": "message", "id": "a", "parentId": "u", "message": {
            "role": "assistant", "content": [
                {"type": "text", "text": "Starting a review."},
                {"type": "toolCall", "id": "review-call", "name": "subagent_run",
                 "arguments": {"agent": "general-reviewer", "task": "Review the change", "async": True}},
            ],
        }},
        {"type": "message", "id": "r", "parentId": "a", "message": {
            "role": "toolResult", "toolCallId": "review-call", "toolName": "subagent_run",
            "content": [{"type": "text", "text": "Async: general-reviewer [detached-id]"}],
        }},
        {"type": "custom_message", "id": "c", "parentId": "r",
         "customType": "subagent-async-completion", "content": "Review: missing cancellation guard."},
    ]
    parent.write_text("\n".join(json.dumps(entry) for entry in entries))
    child = tmp_path / "parent" / "different-child-id" / "run-0" / "session.jsonl"
    child.parent.mkdir(parents=True)
    child.write_text("\n".join(json.dumps(entry) for entry in [
        {"type": "session", "id": "navigation-child", "cwd": str(tmp_path)},
        {"type": "message", "id": "u", "parentId": None, "message": {
            "role": "user", "content": [{"type": "text", "text": "Review the change"}],
        }},
        {"type": "message", "id": "a", "parentId": "u", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "Review: missing cancellation guard."}],
        }},
    ]))

    result = indexer.index_source_transcript("pi", str(parent), indexer.NO_SUMMARY_INDEX_OPTIONS)
    text = Path(result.transcript_path).read_text()
    links = navigation_paths(text)
    child_dir = next(path for path in links if path.is_dir())
    children = list(child_dir.glob("agent-*.md"))
    assert len(children) == result.subagents == 1
    assert "Review the change" in children[0].read_text()
    assert "missing cancellation guard" in children[0].read_text()
    assert "missing cancellation guard" not in text  # Navigation does not inline child output.
    assert children[0].name not in text  # The child list is progressively disclosed.
    assert "detached-id" in next(path for path in links if path.is_file()).read_text()

    parsed = indexer.parse_session_file("pi", str(parent))
    assert transcript.read_assistant_metrics(result.transcript_path) == transcript.assistant_metrics(parsed.messages)
    assert transcript.extract_evidence_snippets(result.transcript_path, ["artifacts"]) == []


def test_clean_only_regeneration_reports_actual_sibling_availability(storage):
    messages = [{"role": "user", "content": "Investigate"}, {"role": "assistant", "content": "Done"}]
    path = Path(transcript.write_transcript("pi:test", messages))
    first = path.read_text()
    links = navigation_paths(first)
    assert len(links) == 2
    assert all(not sibling.exists() for sibling in links)
    assert "(available)" not in first

    tool_path = next(sibling for sibling in links if sibling.suffix == ".md")
    tool_path.write_text("Recorded tool result")
    directory = next(sibling for sibling in links if sibling != tool_path)
    directory.mkdir()
    (directory / "agent-existing.md").write_text("Existing child result")
    # A directory matching the filename pattern is not an available transcript.
    (directory / "agent-not-a-file.md").mkdir()
    transcript.write_transcript("pi:test", messages)
    second = path.read_text()
    assert navigation_paths(second) == links
    assert "(available)" in second
    assert "(1 available)" in second
    assert first.split("[user]", 1)[1] == second.split("[user]", 1)[1]
