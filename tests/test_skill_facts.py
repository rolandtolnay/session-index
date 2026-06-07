import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import indexer
from evidence_find import find_candidates
from parser import ParsedToolCall
from skill_facts import build_skill_invocation_rows
from subagent_runs import ParsedSubagentRun


def _isolate_storage(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    transcript_dir = data_dir / "transcripts"
    monkeypatch.setattr(db, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(db, "DB_PATH", str(data_dir / "sessions.db"))

    import transcript
    import tool_log

    monkeypatch.setattr(transcript, "TRANSCRIPT_DIR", str(transcript_dir))
    monkeypatch.setattr(tool_log, "TRANSCRIPT_DIR", str(transcript_dir))


def _pi_session(tmp_path, user_text: str, *, session_id: str = "019skill-0001"):
    path = tmp_path / f"{session_id}.jsonl"
    lines = [
        {"type": "session", "version": 3, "id": session_id, "timestamp": "2026-04-01T10:00:00.000Z", "cwd": str(tmp_path)},
        {"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-04-01T10:00:01.000Z", "message": {"role": "user", "content": user_text}},
        {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "2026-04-01T10:00:02.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def test_pi_skill_envelope_indexes_as_skill_invocation_without_storing_body(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    source = _pi_session(
        tmp_path,
        '<skill name="review" version="1"><objective>Very long expanded prompt body that must not be stored.</objective></skill>',
    )

    result = indexer.index_source_transcript("pi", str(source), indexer.NO_SUMMARY_INDEX_OPTIONS)

    conn = db.get_connection()
    data = find_candidates(conn, skill="review", session=result.session_id)
    row = conn.execute("SELECT * FROM skill_invocations WHERE session_id=?", (result.session_id,)).fetchone()
    conn.close()

    assert data["results"][0]["ref"] == f"skill/{result.session_id}/1"
    assert data["results"][0]["match"] == {
        "kind": "skill_invocation",
        "sequence": 1,
        "skill_name": "review",
        "timestamp": "2026-04-01T10:00:01.000Z",
        "invocation_preview": '<skill name="review">',
    }
    assert row["skill_name"] == "review"
    assert "expanded prompt body" not in (row["invocation_preview"] or "")
    assert "expanded prompt body" not in (row["arguments"] or "")


def test_slash_commands_canonicalize_names_and_exclude_lifecycle_commands(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    source = _pi_session(tmp_path, "/skill:Review changed files\n/clear\n[/plan-quick] implement this")

    result = indexer.index_source_transcript("pi", str(source), indexer.NO_SUMMARY_INDEX_OPTIONS)

    conn = db.get_connection()
    rows = conn.execute(
        "SELECT sequence, skill_name, arguments FROM skill_invocations WHERE session_id=? ORDER BY sequence",
        (result.session_id,),
    ).fetchall()
    conn.close()

    assert [tuple(row) for row in rows] == [
        (1, "review", "changed files"),
        (2, "plan-quick", "implement this"),
    ]


def test_message_invocations_preserve_source_order_within_same_timestamp():
    rows = build_skill_invocation_rows(
        "s1",
        "pi",
        [{"role": "user", "timestamp": "2026-01-01T00:00:00Z", "content": "/review changed\n<skill name=\"plan-quick\">body</skill>"}],
        [],
        [],
    )

    assert [(row["sequence"], row["skill_name"]) for row in rows] == [(1, "review"), (2, "plan-quick")]


def test_absolute_paths_are_not_slash_command_invocations():
    rows = build_skill_invocation_rows(
        "s1",
        "pi",
        [{"role": "user", "timestamp": "2026-01-01T00:00:00Z", "content": "/Users/rolandtolnay/Desktop/Screenshot\\ 2026-06-07\\ at\\ 10.36.01.png"}],
        [],
        [],
    )

    assert rows == []


def test_bracket_command_examples_inside_prose_are_not_invocations():
    rows = build_skill_invocation_rows(
        "s1",
        "pi",
        [{"role": "user", "timestamp": "2026-01-01T00:00:00Z", "content": "Example syntax: [/review] changed files"}],
        [],
        [],
    )

    assert rows == []


def test_provider_skill_tool_call_builds_canonical_invocation_row():
    rows = build_skill_invocation_rows(
        "s1",
        "claude",
        [],
        [ParsedToolCall(sequence=7, timestamp="2026-01-01T00:00:00Z", tool_name="Skill", arguments={"skill": "Review", "notes": "ignore"})],
        [],
    )

    assert rows == [{
        "session_id": "s1",
        "source": "claude",
        "sequence": 1,
        "timestamp": "2026-01-01T00:00:00Z",
        "skill_name": "review",
        "invocation_preview": None,
        "arguments": None,
        "transcript_message_index": None,
        "tool_sequence": 7,
        "child_index": None,
        "subagent_transcript_path": None,
    }]


def test_provider_skill_tool_call_in_subagent_scope_keeps_locator_metadata():
    rows = build_skill_invocation_rows(
        "s1",
        "claude",
        [],
        [ParsedToolCall(sequence=7, timestamp="2026-01-01T00:00:00Z", scope="agent-child", tool_name="Skill", arguments={"skill": "Review"})],
        [ParsedSubagentRun(parent_session_id="s1", source="claude", requested_agent_type="worker", child_index=0, agent_id="child", transcript_path="/tmp/child.md", call_tool="Agent")],
    )

    assert [(row["skill_name"], row["tool_sequence"], row["child_index"], row["subagent_transcript_path"]) for row in rows] == [
        ("review", 7, 0, "/tmp/child.md"),
    ]


def test_exact_skill_md_reads_build_invocations_and_subagent_locator_metadata():
    rows = build_skill_invocation_rows(
        "s1",
        "pi",
        [],
        [
            ParsedToolCall(sequence=2, timestamp="2026-01-01T00:00:01Z", scope="main", tool_name="read", arguments={"path": "/Users/me/.pi/agent/skills/review/SKILL.md"}),
            ParsedToolCall(sequence=3, timestamp="2026-01-01T00:00:02Z", scope="agent-child", tool_name="Read", arguments={"file_path": "/Users/me/.pi/agent/skills/diagnose/SKILL.md"}),
        ],
        [ParsedSubagentRun(parent_session_id="s1", source="pi", requested_agent_type="worker", child_index=0, agent_id="child", transcript_path="/tmp/child.md", call_tool="subagent_run")],
    )

    assert [(row["sequence"], row["skill_name"], row["tool_sequence"], row["child_index"], row["subagent_transcript_path"]) for row in rows] == [
        (1, "review", 2, None, None),
        (2, "diagnose", 3, 0, "/tmp/child.md"),
    ]


def test_nested_tool_use_exact_skill_md_read_builds_invocation():
    rows = build_skill_invocation_rows(
        "s1",
        "pi",
        [],
        [ParsedToolCall(
            sequence=9,
            timestamp="2026-01-01T00:00:00Z",
            tool_name="multi_tool_use.parallel",
            arguments={"tool_uses": [{
                "recipient_name": "functions.read",
                "parameters": {"path": "/Users/me/.pi/agent/skills/review/SKILL.md"},
            }]},
        )],
        [],
    )

    assert [(row["skill_name"], row["tool_sequence"]) for row in rows] == [("review", 9)]
