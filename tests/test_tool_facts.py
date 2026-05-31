"""Tests for structured fact extraction (tool_facts.py)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import ParsedQuestionSelection, ParsedToolCall
from subagent_runs import ParsedSubagentRun
from tool_facts import (
    build_file_mutation_rows,
    build_question_rows,
    build_subagent_run_rows,
    build_tool_call_rows,
    extract_skill_name,
    normalize_tool_name,
)


# ── normalize_tool_name / extract_skill_name ────────────────────────────────

def test_normalize_tool_name_strips_namespace_and_lowercases():
    assert normalize_tool_name("Agent") == "agent"
    assert normalize_tool_name("AskUserQuestion") == "askuserquestion"
    assert normalize_tool_name("bash") == "bash"
    assert normalize_tool_name("mcp.namespace.subagent_run") == "subagent_run"
    assert normalize_tool_name("") == ""


def test_extract_skill_name_only_for_skill_tool():
    assert extract_skill_name(ParsedToolCall(tool_name="Skill", arguments={"skill": "update-config"})) == "update-config"
    assert extract_skill_name(ParsedToolCall(tool_name="bash", arguments={"skill": "x"})) is None
    assert extract_skill_name(ParsedToolCall(tool_name="Skill", arguments={})) is None


# ── build_tool_call_rows ────────────────────────────────────────────────────

def test_build_tool_call_rows_normalizes_and_flags():
    calls = [
        ParsedToolCall(scope="main", sequence=1, tool_name="Agent", arguments={"subagent_type": "Explore"}),
        ParsedToolCall(scope="agent-x", sequence=2, tool_name="Bash", is_error=True),
        ParsedToolCall(scope="main", sequence=3, tool_name="Skill", arguments={"skill": "review"}),
    ]
    rows = build_tool_call_rows("sess-1", "claude", calls)

    assert [r["tool"] for r in rows] == ["agent", "bash", "skill"]
    assert [r["scope"] for r in rows] == ["main", "agent-x", "main"]
    assert [r["is_error"] for r in rows] == [0, 1, 0]
    assert rows[2]["skill_name"] == "review"
    assert rows[0]["skill_name"] is None
    assert all(r["session_id"] == "sess-1" and r["source"] == "claude" for r in rows)


# ── build_file_mutation_rows ────────────────────────────────────────────────


def test_claude_write_file_path_produces_file_mutation_row():
    call = ParsedToolCall(
        scope="main",
        sequence=7,
        timestamp="2026-05-31T12:00:00Z",
        tool_name="Write",
        arguments={"file_path": "src/app.py", "content": "print('hi')"},
    )

    rows = build_file_mutation_rows("sess-1", "claude", [call])

    assert rows == [{
        "session_id": "sess-1",
        "source": "claude",
        "scope": "main",
        "sequence": 7,
        "timestamp": "2026-05-31T12:00:00Z",
        "tool_name": "Write",
        "tool": "write",
        "path": "src/app.py",
    }]


def test_claude_edit_file_path_produces_file_mutation_row():
    call = ParsedToolCall(tool_name="Edit", arguments={"file_path": "src/app.py", "old_string": "a", "new_string": "b"})

    rows = build_file_mutation_rows("sess-1", "claude", [call])

    assert [(r["tool_name"], r["tool"], r["path"]) for r in rows] == [("Edit", "edit", "src/app.py")]


def test_pi_write_and_edit_path_produce_file_mutation_rows():
    calls = [
        ParsedToolCall(sequence=1, tool_name="write", arguments={"path": "src/a.py", "content": "a"}),
        ParsedToolCall(sequence=2, tool_name="edit", arguments={"path": "src/b.py", "oldText": "a", "newText": "b"}),
    ]

    rows = build_file_mutation_rows("pi:sess-1", "pi", calls)

    assert [(r["sequence"], r["tool"], r["path"]) for r in rows] == [
        (1, "write", "src/a.py"),
        (2, "edit", "src/b.py"),
    ]


def test_pi_edit_nested_paths_used_when_no_top_level_path_covers_them():
    call = ParsedToolCall(
        sequence=3,
        tool_name="edit",
        arguments={"edits": [{"path": "src/a.py", "oldText": "a"}, {"path": "src/b.py", "oldText": "b"}]},
    )

    rows = build_file_mutation_rows("pi:sess-1", "pi", [call])

    assert [(r["tool"], r["path"]) for r in rows] == [("edit", "src/a.py"), ("edit", "src/b.py")]


def test_edit_top_level_and_nested_paths_are_both_preserved_when_distinct():
    call = ParsedToolCall(
        tool_name="edit",
        arguments={"path": "src/top.py", "edits": [{"path": "src/nested.py"}]},
    )

    rows = build_file_mutation_rows("pi:sess-1", "pi", [call])

    assert [r["path"] for r in rows] == ["src/top.py", "src/nested.py"]


def test_wrapper_write_and_edit_mutations_use_wrapper_attribution_and_nested_tool_names():
    call = ParsedToolCall(
        scope="main",
        sequence=9,
        timestamp="2026-05-31T13:00:00Z",
        tool_name="multi_tool_use.parallel",
        arguments={"tool_uses": [
            {"recipient_name": "functions.write", "parameters": {"path": "src/a.py", "content": "a"}},
            {"recipient_name": "functions.edit", "parameters": {"path": "src/b.py", "edits": []}},
            {"recipient_name": "functions.read", "parameters": {"path": "src/c.py"}},
        ]},
    )

    rows = build_file_mutation_rows("sess-1", "pi", [call])

    assert all(
        row["session_id"] == "sess-1"
        and row["source"] == "pi"
        and row["scope"] == "main"
        and row["sequence"] == 9
        and row["timestamp"] == "2026-05-31T13:00:00Z"
        for row in rows
    )
    assert [(row["tool_name"], row["tool"], row["path"]) for row in rows] == [
        ("functions.write", "write", "src/a.py"),
        ("functions.edit", "edit", "src/b.py"),
    ]


def test_failed_file_mutations_are_excluded():
    calls = [
        ParsedToolCall(tool_name="Write", arguments={"file_path": "src/a.py"}, is_error=True),
        ParsedToolCall(tool_name="edit", arguments={"path": "src/b.py"}, is_error=True),
    ]

    assert build_file_mutation_rows("sess-1", "claude", calls) == []


def test_non_mutating_tools_do_not_produce_file_mutation_rows():
    calls = [
        ParsedToolCall(tool_name="Read", arguments={"file_path": "src/a.py"}),
        ParsedToolCall(tool_name="Grep", arguments={"path": "src"}),
        ParsedToolCall(tool_name="LS", arguments={"path": "src"}),
        ParsedToolCall(tool_name="Bash", arguments={"command": "cat > src/a.py"}),
    ]

    assert build_file_mutation_rows("sess-1", "claude", calls) == []


def test_file_mutations_dedupe_paths_within_call_but_not_across_calls():
    calls = [
        ParsedToolCall(sequence=1, tool_name="edit", arguments={"edits": [{"path": "src/a.py"}, {"path": "src/a.py"}]}),
        ParsedToolCall(sequence=2, tool_name="edit", arguments={"path": "src/a.py"}),
    ]

    rows = build_file_mutation_rows("sess-1", "pi", calls)

    assert [(r["sequence"], r["path"]) for r in rows] == [(1, "src/a.py"), (2, "src/a.py")]


def test_file_mutations_preserve_subagent_scope():
    call = ParsedToolCall(scope="agent-a5f64306c4e829331", sequence=4, tool_name="Edit", arguments={"file_path": "src/agent.py"})

    rows = build_file_mutation_rows("sess-1", "claude", [call])

    assert rows[0]["scope"] == "agent-a5f64306c4e829331"
    assert rows[0]["path"] == "src/agent.py"


# ── build_question_rows ─────────────────────────────────────────────────────

def _claude_question_call(question, options, answer_label, multi=False):
    """Build a Claude question call whose result echoes the picked label."""
    result = f'Your questions have been answered: "{question}"="{answer_label}". You can now continue.'
    return ParsedToolCall(
        sequence=1,
        tool_name="AskUserQuestion",
        arguments={"questions": [{"question": question, "header": "H", "multiSelect": multi, "options": options}]},
        result=result,
    )


def test_claude_recommended_label_match():
    options = [
        {"label": "Use approach A (Recommended)", "description": "first"},
        {"label": "Use approach B", "description": "second"},
    ]
    call = _claude_question_call("Which approach?", options, "Use approach A (Recommended)")
    rows = build_question_rows("s", "claude", [call])

    assert len(rows) == 1
    row = rows[0]
    assert row["selected_label"] == "Use approach A (Recommended)"
    assert row["was_recommended"] == 1
    assert row["is_other"] == 0
    assert row["option_count"] == 2
    assert row["multi_select"] == 0


def test_claude_recommended_not_picked():
    options = [
        {"label": "Use approach A (Recommended)", "description": "first"},
        {"label": "Use approach B", "description": "second"},
    ]
    call = _claude_question_call("Which approach?", options, "Use approach B")
    rows = build_question_rows("s", "claude", [call])

    assert rows[0]["was_recommended"] == 0
    assert rows[0]["is_other"] == 0


def test_claude_recommended_in_description():
    options = [
        {"label": "Approach A", "description": "first (Recommended)"},
        {"label": "Approach B", "description": "second"},
    ]
    call = _claude_question_call("Which approach?", options, "Approach A")
    rows = build_question_rows("s", "claude", [call])

    assert rows[0]["was_recommended"] == 1


def test_claude_other_answer_is_flagged():
    options = [
        {"label": "Approach A (Recommended)", "description": "first"},
        {"label": "Approach B", "description": "second"},
    ]
    call = _claude_question_call("Which approach?", options, "Something custom I typed")
    rows = build_question_rows("s", "claude", [call])

    assert rows[0]["is_other"] == 1
    assert rows[0]["was_recommended"] == 0


def test_no_recommended_option_yields_null():
    options = [{"label": "Approach A", "description": "first"}, {"label": "Approach B", "description": "second"}]
    call = _claude_question_call("Which approach?", options, "Approach A")
    rows = build_question_rows("s", "claude", [call])

    assert rows[0]["was_recommended"] is None
    assert rows[0]["is_other"] == 0


def test_pi_answer_resolved_from_parser_normalized_question_selection():
    options = [
        {"label": "Future only (Recommended)", "description": "later"},
        {"label": "Future + existing", "description": "now"},
    ]
    question = "Cover only future, or also existing?"
    call = ParsedToolCall(
        sequence=4,
        tool_name="question",
        arguments={"questions": [{"question": question, "header": "Scope", "multiSelect": False, "options": options}]},
        result="",  # legacy empty content — resolve from parser-normalized outcome
        question_selections=[ParsedQuestionSelection(question=question, selected_labels=["Future + existing"])],
    )
    rows = build_question_rows("pi:s", "pi", [call])

    assert rows[0]["selected_label"] == "Future + existing"
    assert rows[0]["was_recommended"] == 0
    assert rows[0]["is_other"] == 0
    assert rows[0]["sequence"] == 4


def test_cancelled_question_stored_unanswered():
    options = [{"label": "A (Recommended)", "description": "x"}, {"label": "B", "description": "y"}]
    question = "Pick one?"
    call = ParsedToolCall(
        tool_name="question",
        arguments={"questions": [{"question": question, "header": "H", "multiSelect": False, "options": options}]},
        result="The question was cancelled.",
        question_cancelled=True,
    )
    rows = build_question_rows("pi:s", "pi", [call])

    assert rows[0]["selected_label"] is None
    assert rows[0]["was_recommended"] is None
    assert rows[0]["is_other"] is None


def test_unanswered_question_with_no_signal_stored_null():
    options = [{"label": "A", "description": "x"}]
    call = ParsedToolCall(
        tool_name="AskUserQuestion",
        arguments={"questions": [{"question": "Q?", "header": "H", "multiSelect": False, "options": options}]},
        result="",
    )
    rows = build_question_rows("s", "claude", [call])

    assert rows[0]["selected_label"] is None
    assert rows[0]["was_recommended"] is None


def test_multiselect_stores_joined_labels_with_null_recommended():
    options = [{"label": "Tags", "description": "x"}, {"label": "Titles", "description": "y"}, {"label": "Body", "description": "z"}]
    question = "Which fields?"
    call = ParsedToolCall(
        tool_name="question",
        arguments={"questions": [{"question": question, "header": "H", "multiSelect": True, "options": options}]},
        question_selections=[ParsedQuestionSelection(question=question, selected_labels=["Tags", "Titles"])],
    )
    rows = build_question_rows("pi:s", "pi", [call])

    assert rows[0]["multi_select"] == 1
    assert rows[0]["selected_label"] == "Tags, Titles"
    assert rows[0]["was_recommended"] is None
    assert rows[0]["is_other"] == 0


def test_non_question_calls_ignored():
    rows = build_question_rows("s", "claude", [ParsedToolCall(tool_name="Bash", arguments={"command": "ls"})])
    assert rows == []


# ── build_subagent_run_rows ─────────────────────────────────────────────────

def test_build_subagent_run_rows_maps_fields():
    runs = [ParsedSubagentRun(
        parent_session_id="p1",
        source="claude",
        requested_agent_type="Explore",
        observed_agent_type="general-purpose",
        call_tool="Agent",
        call_sequence=3,
        call_tool_id="tool-1",
        child_index=0,
        agent_id="a1",
        tool_call_count=5,
        transcript_path="/tmp/agent-a1.md",
        task_preview="Map the auth flow",
        match_confidence="ordered",
    )]
    rows = build_subagent_run_rows(runs)

    assert len(rows) == 1
    row = rows[0]
    assert row["parent_session_id"] == "p1"
    assert row["requested_agent_type"] == "Explore"
    assert row["observed_agent_type"] == "general-purpose"
    assert row["call_sequence"] == 3
    assert row["tool_call_count"] == 5
    assert row["match_confidence"] == "ordered"
