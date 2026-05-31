import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inspect_refs import InspectionRefError, QuestionRef, SessionRef, SubagentRef, ToolRef, format_ref, parse_ref


def test_parse_session_ref_with_pi_canonical_id():
    ref = parse_ref("session/pi:abc-123")
    assert ref == SessionRef(session_id="pi:abc-123")
    assert format_ref(ref) == "session/pi:abc-123"


def test_parse_tool_ref():
    ref = parse_ref("tool/pi:abc/12")
    assert ref == ToolRef(session_id="pi:abc", sequence=12)
    assert format_ref(ref) == "tool/pi:abc/12"


def test_parse_question_ref():
    ref = parse_ref("question/pi:abc/12/0")
    assert ref == QuestionRef(session_id="pi:abc", sequence=12, question_index=0)
    assert format_ref(ref) == "question/pi:abc/12/0"


def test_parse_subagent_ref():
    ref = parse_ref("subagent/pi:abc/2")
    assert ref == SubagentRef(session_id="pi:abc", child_index=2)
    assert format_ref(ref) == "subagent/pi:abc/2"


@pytest.mark.parametrize("value,message", [
    ("", "non-empty"),
    ("file/tmp/x", "Unknown"),
    ("tool/pi:abc/not-int", "Invalid sequence"),
    ("tool/pi:abc", "Expected tool"),
    ("question/pi:abc/1/nope", "Invalid question_index"),
    ("subagent/pi:abc/-1", "non-negative"),
])
def test_invalid_refs_raise_clear_errors(value, message):
    with pytest.raises(InspectionRefError, match=message):
        parse_ref(value)
