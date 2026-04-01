"""Tests for the transcript writer."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transcript import write_transcript


def test_write_transcript(tmp_path, monkeypatch):
    # Redirect transcript dir to tmp
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))

    messages = [
        {"role": "user", "content": "Fix the bug"},
        {"role": "assistant", "content": "I found the issue in the login handler."},
        {"role": "user", "content": "Looks good"},
        {"role": "assistant", "content": "Done!"},
    ]

    path = write_transcript(
        "test-session",
        messages,
        slug="test-slug",
        project="myproject",
        branch="main",
        timestamp="2026-04-01T14:23:45Z",
    )

    assert os.path.exists(path)
    content = open(path).read()
    # Header
    assert "test-slug" in content
    assert "myproject" in content
    assert "main" in content
    assert "2026-04-01" in content
    assert "---" in content
    # Messages
    assert "User: Fix the bug" in content
    assert "Assistant: I found the issue" in content
    assert "User: Looks good" in content
    assert "Assistant: Done!" in content


def test_write_transcript_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    path = write_transcript("empty-session", [])
    assert os.path.exists(path)
    content = open(path).read()
    # Header still present even with no messages
    assert "---" in content
