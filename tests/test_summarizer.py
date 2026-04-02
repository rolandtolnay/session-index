"""Tests for the summarizer message selection."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from summarizer import _select_messages, _build_prompt


def test_short_list_unchanged():
    msgs = [f"msg{i}" for i in range(20)]
    assert _select_messages(msgs) == msgs


def test_exact_budget_unchanged():
    msgs = [f"msg{i}" for i in range(30)]
    assert _select_messages(msgs) == msgs


def test_long_list_respects_budget():
    msgs = [f"msg{i}" for i in range(100)]
    result = _select_messages(msgs)
    assert len(result) <= 30


def test_long_list_keeps_first_and_last():
    msgs = [f"msg{i}" for i in range(100)]
    result = _select_messages(msgs)
    assert result[:5] == msgs[:5]
    assert result[-5:] == msgs[-5:]


def test_long_list_samples_middle():
    msgs = [f"msg{i}" for i in range(100)]
    result = _select_messages(msgs)
    middle = result[5:-5]
    # Middle messages should come from msgs[5:-5] range
    for m in middle:
        idx = int(m.replace("msg", ""))
        assert 5 <= idx <= 94


# --- _build_prompt with last assistant message ---


def test_build_prompt_without_assistant():
    prompt = _build_prompt("proj", "main", ["fix the bug"], ["app.py"])
    assert "Last assistant response" not in prompt
    assert "fix the bug" in prompt


def test_build_prompt_with_assistant():
    prompt = _build_prompt(
        "proj", "main", ["what is this code doing?"], ["app.py"],
        last_assistant_message="This code handles authentication by...",
    )
    assert "Last assistant response:" in prompt
    assert "This code handles authentication by..." in prompt


def test_build_prompt_truncates_long_assistant():
    long_msg = "x" * 1000
    prompt = _build_prompt(
        "proj", "main", ["explain this"], [],
        last_assistant_message=long_msg,
    )
    assert "x" * 500 in prompt
    assert "x" * 501 not in prompt
    assert "..." in prompt


def test_build_prompt_none_assistant_same_as_omitted():
    p1 = _build_prompt("proj", "main", ["msg"], [])
    p2 = _build_prompt("proj", "main", ["msg"], [], last_assistant_message=None)
    assert p1 == p2
