"""Artifact text is untrusted; clipboard access is an explicit system boundary."""

import subprocess
from unittest.mock import Mock

import pytest

import manage_artifacts
from manage_artifacts import copy_transcript_path, read_transcript


def test_read_preserves_conversation_structure_without_terminal_controls(tmp_path):
    path = tmp_path / "side-chat.md"
    path.write_text("# Focused content\n\n    code()\n\tindent\n[user] café 界\n\x1b[31manswer\x07\x9b\x00\n")
    text = read_transcript(str(path))
    assert "\n\n    code()\n\tindent\n[user] café 界\n" in text
    assert "answer" in text
    assert all(char.isprintable() or char in "\n\t" for char in text)


def test_read_reports_missing_and_invalid_transcripts(tmp_path):
    path = tmp_path / "missing.md"
    with pytest.raises(OSError):
        read_transcript(str(path))
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        read_transcript(str(path))
    with pytest.raises(ValueError, match="absolute"):
        read_transcript("relative.md")


def test_copy_sends_exact_path_as_data_without_newline(monkeypatch):
    path = "/tmp/Side Chats/café;$(echo unsafe).md"
    run = Mock(return_value=subprocess.CompletedProcess(["pbcopy"], 0))
    monkeypatch.setattr(manage_artifacts.sys, "platform", "darwin")
    monkeypatch.setattr(manage_artifacts.subprocess, "run", run)
    copy_transcript_path(path)
    args, kwargs = run.call_args
    assert args == (["pbcopy"],)
    assert kwargs["input"] == path.encode("utf-8")
    assert kwargs["timeout"] > 0
    assert not kwargs.get("shell")


@pytest.mark.parametrize("failure", [
    FileNotFoundError(), subprocess.TimeoutExpired("pbcopy", 3),
    subprocess.CompletedProcess(["pbcopy"], 1),
])
def test_copy_failure_is_reported(monkeypatch, failure):
    run = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    monkeypatch.setattr(manage_artifacts.sys, "platform", "darwin")
    monkeypatch.setattr(manage_artifacts.subprocess, "run", run)
    with pytest.raises(OSError, match="Clipboard"):
        copy_transcript_path("/tmp/chat.md")


def test_copy_unsupported_platform_never_launches_clipboard(monkeypatch):
    run = Mock()
    monkeypatch.setattr(manage_artifacts.sys, "platform", "linux")
    monkeypatch.setattr(manage_artifacts.subprocess, "run", run)
    with pytest.raises(OSError, match="macOS"):
        copy_transcript_path("/tmp/chat.md")
    run.assert_not_called()
