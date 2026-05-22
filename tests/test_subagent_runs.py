import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import ParsedToolCall
from subagent_parser import ParsedSubagent
from subagent_runs import build_subagent_runs


def test_claude_agent_tool_call_creates_requested_agent_fact():
    facts = build_subagent_runs(
        parent_session_id="parent-1",
        source="claude",
        tool_calls=[ParsedToolCall(
            tool_call_id="tool-1",
            tool_name="Agent",
            arguments={"subagent_type": "general-purpose", "prompt": "Inspect the code"},
        )],
        subagents=[],
    )

    assert len(facts) == 1
    assert facts[0].requested_agent_type == "general-purpose"
    assert facts[0].call_tool == "Agent"
    assert facts[0].call_tool_id == "tool-1"
    assert facts[0].match_confidence == "request_only"


def test_pi_subagent_parallel_creates_one_fact_per_child_task():
    facts = build_subagent_runs(
        parent_session_id="pi:parent-1",
        source="pi",
        tool_calls=[ParsedToolCall(
            tool_call_id="call-1",
            tool_name="subagent_parallel",
            arguments={
                "tasks": [
                    {"agent": "scout", "task": "Map files"},
                    {"agent": "worker", "task": "Implement change"},
                ]
            },
        )],
        subagents=[],
    )

    assert [fact.requested_agent_type for fact in facts] == ["scout", "worker"]
    assert [fact.task_preview for fact in facts] == ["Map files", "Implement change"]


def test_pi_generic_child_artifacts_keep_parent_requested_names_when_matched():
    facts = build_subagent_runs(
        parent_session_id="pi:parent-1",
        source="pi",
        tool_calls=[ParsedToolCall(
            tool_name="subagent_parallel",
            arguments={"tasks": [{"agent": "scout"}, {"agent": "worker"}]},
        )],
        subagents=[
            ParsedSubagent(agent_id="run-1", agent_type="subagent", tool_call_count=2, transcript_path="/tmp/agent-run-1.md"),
            ParsedSubagent(agent_id="run-2", agent_type="subagent", tool_call_count=3, transcript_path="/tmp/agent-run-2.md"),
        ],
    )

    assert [fact.requested_agent_type for fact in facts] == ["scout", "worker"]
    assert [fact.observed_agent_type for fact in facts] == ["subagent", "subagent"]
    assert [fact.agent_id for fact in facts] == ["run-1", "run-2"]
    assert [fact.transcript_path for fact in facts] == ["/tmp/agent-run-1.md", "/tmp/agent-run-2.md"]
    assert all(fact.match_confidence == "ordered" for fact in facts)


def test_agent_description_is_task_preview_not_requested_agent_type():
    facts = build_subagent_runs(
        parent_session_id="parent-1",
        source="claude",
        tool_calls=[ParsedToolCall(
            tool_name="Agent",
            arguments={"description": "Research authentication options"},
        )],
        subagents=[],
    )

    assert facts[0].requested_agent_type == "Agent"
    assert facts[0].task_preview == "Research authentication options"


def test_management_tools_do_not_create_subagent_run_facts():
    facts = build_subagent_runs(
        parent_session_id="pi:parent-1",
        source="pi",
        tool_calls=[
            ParsedToolCall(tool_name="subagents_list", arguments={}),
            ParsedToolCall(tool_name="subagent_status", arguments={"id": "run"}),
        ],
        subagents=[],
    )

    assert facts == []
