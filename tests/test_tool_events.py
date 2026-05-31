import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import ParsedToolCall
from subagent_parser import ParsedSubagent
from tool_events import combine_tool_calls


def test_combine_tool_calls_sequences_main_before_subagents():
    main = [ParsedToolCall(tool_name="read", tool_call_id="main-call")]
    sub = ParsedSubagent(agent_id="abc123", tool_calls=[
        ParsedToolCall(tool_name="bash", tool_call_id="sub-call")
    ])

    combined = combine_tool_calls(main, [sub])

    assert [(c.sequence, c.scope, c.tool_call_id) for c in combined] == [
        (1, "main", "main-call"),
        (2, "agent-abc123", "sub-call"),
    ]
