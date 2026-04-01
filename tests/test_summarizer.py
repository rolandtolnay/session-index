"""Tests for the summarizer message selection."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from summarizer import _select_messages


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
