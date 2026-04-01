"""Tests for the transcript writer."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transcript import write_transcript, extract_excerpts


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
    # Messages with fenced delimiters
    assert "[user]" in content
    assert "Fix the bug" in content
    assert "[assistant]" in content
    assert "I found the issue" in content
    assert "Looks good" in content
    assert "Done!" in content


def test_write_transcript_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path))
    path = write_transcript("empty-session", [])
    assert os.path.exists(path)
    content = open(path).read()
    # Header still present even with no messages
    assert "---" in content


# ── Excerpt extraction tests ────────────────────────────────────────────────


def _write_test_transcript(tmp_path, messages):
    """Helper: write a transcript and return its path."""
    path = str(tmp_path / "test.md")
    lines = ["test | project | 2026-04-01", "---", ""]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "user":
            lines.append(f"[user] {'─' * 40}")
        else:
            lines.append(f"[assistant] {'─' * 34}")
        lines.append(content)
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def test_excerpt_matches_keyword(tmp_path):
    path = _write_test_transcript(tmp_path, [
        {"role": "user", "content": "Fix the authentication timeout bug"},
        {"role": "assistant", "content": "I found the issue in the auth module."},
        {"role": "user", "content": "Add pagination to the API"},
        {"role": "assistant", "content": "Done with pagination."},
    ])
    result = extract_excerpts(path, ["authentication"])
    assert result is not None
    assert "authentication timeout" in result
    assert "pagination" not in result


def test_excerpt_returns_full_block(tmp_path):
    path = _write_test_transcript(tmp_path, [
        {"role": "user", "content": "Fix the auth bug"},
        {"role": "assistant", "content": "The auth module had a token expiry issue.\nI updated the TTL to 1 hour."},
    ])
    result = extract_excerpts(path, ["auth"])
    assert result is not None
    # Both blocks should match (both contain "auth")
    assert "Fix the auth bug" in result
    assert "token expiry issue" in result
    assert "TTL to 1 hour" in result


def test_excerpt_caps_at_max_blocks(tmp_path):
    messages = []
    for i in range(10):
        messages.append({"role": "user", "content": f"auth question {i}"})
        messages.append({"role": "assistant", "content": f"auth answer {i}"})
    path = _write_test_transcript(tmp_path, messages)
    result = extract_excerpts(path, ["auth"], max_blocks=3)
    assert result is not None
    # Should only have 3 blocks, not all 20
    blocks = [b for b in result.split("\n\n") if b.strip()]
    assert len(blocks) <= 3


def test_excerpt_no_match_returns_none(tmp_path):
    path = _write_test_transcript(tmp_path, [
        {"role": "user", "content": "Fix the login bug"},
        {"role": "assistant", "content": "Done."},
    ])
    result = extract_excerpts(path, ["pagination"])
    assert result is None


def test_excerpt_missing_file_returns_none():
    result = extract_excerpts("/nonexistent/path.md", ["auth"])
    assert result is None


def test_excerpt_skips_short_keywords(tmp_path):
    path = _write_test_transcript(tmp_path, [
        {"role": "user", "content": "Fix the auth bug"},
    ])
    # Keywords with <= 2 chars should be skipped
    result = extract_excerpts(path, ["an", "to"])
    assert result is None
