"""Tests for exact current-session env resolution."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from current_session import CurrentSessionError, resolve_current_session


REQUIRED_ENV_KEYS = [
    "SESSION_INDEX_SESSION_ID",
    "SESSION_INDEX_NATIVE_SESSION_ID",
    "SESSION_INDEX_SOURCE",
    "SESSION_INDEX_SOURCE_PATH",
]

CLAUDE_COMPAT_ENV_KEYS = [
    "CLAUDE_SESSION_ID",
    "CLAUDE_TRANSCRIPT_PATH",
    "CLAUDE_CODE_TRANSCRIPT_PATH",
]


def _env(session_id="session-1", native_session_id="session-1", source="claude", source_path="/tmp/source.jsonl", **extra):
    data = {
        "SESSION_INDEX_SESSION_ID": session_id,
        "SESSION_INDEX_NATIVE_SESSION_ID": native_session_id,
        "SESSION_INDEX_SOURCE": source,
        "SESSION_INDEX_SOURCE_PATH": source_path,
    }
    data.update(extra)
    return data


def test_resolve_pi_env_normalizes_ids_and_derives_artifact_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("current_session.transcript.TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("current_session.tool_log.TRANSCRIPT_DIR", str(tmp_path))
    source = tmp_path / "source.jsonl"
    source.write_text("{}\n")
    transcript_path = tmp_path / "pi:019pi-session.md"
    transcript_path.write_text("transcript")

    current = resolve_current_session(_env(
        session_id="pi:019pi-session",
        native_session_id="019pi-session",
        source="pi",
        source_path=str(source),
        SESSION_INDEX_LEAF_ID="leaf-123",
    ))

    assert current.session_id == "pi:019pi-session"
    assert current.native_session_id == "019pi-session"
    assert current.source == "pi"
    assert current.source_path == str(source)
    assert current.transcript_path == str(transcript_path)
    assert current.tool_log_path == str(tmp_path / "pi:019pi-session.tools.md")
    assert current.source_path_exists is True
    assert current.transcript_exists is True
    assert current.tool_log_exists is False
    assert current.resolution_method == "session_index_env"
    assert current.leaf_id == "leaf-123"


def test_resolve_pi_env_adds_canonical_prefix_and_strips_native_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr("current_session.transcript.TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("current_session.tool_log.TRANSCRIPT_DIR", str(tmp_path))

    current = resolve_current_session(_env(
        session_id="019pi-session",
        native_session_id="pi:019pi-session",
        source="pi",
        source_path=str(tmp_path / "missing.jsonl"),
    ))

    assert current.session_id == "pi:019pi-session"
    assert current.native_session_id == "019pi-session"
    assert current.transcript_path == str(tmp_path / "pi:019pi-session.md")
    assert current.tool_log_path == str(tmp_path / "pi:019pi-session.tools.md")


def test_resolve_claude_env_keeps_canonical_and_native_equal(tmp_path, monkeypatch):
    monkeypatch.setattr("current_session.transcript.TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("current_session.tool_log.TRANSCRIPT_DIR", str(tmp_path))
    tool_log_path = tmp_path / "claude-session.tools.md"
    tool_log_path.write_text("tools")

    current = resolve_current_session(_env(
        session_id="claude-session",
        native_session_id="claude-session",
        source="claude",
        source_path=str(tmp_path / "missing.jsonl"),
    ))

    assert current.session_id == "claude-session"
    assert current.native_session_id == "claude-session"
    assert current.transcript_path == str(tmp_path / "claude-session.md")
    assert current.tool_log_path == str(tool_log_path)
    assert current.transcript_exists is False
    assert current.tool_log_exists is True
    assert current.leaf_id is None


def test_resolve_json_dict_includes_public_fields_and_pi_leaf(tmp_path, monkeypatch):
    monkeypatch.setattr("current_session.transcript.TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("current_session.tool_log.TRANSCRIPT_DIR", str(tmp_path))

    current = resolve_current_session(_env(
        session_id="pi:abc",
        native_session_id="abc",
        source="pi",
        source_path=str(tmp_path / "source.jsonl"),
        SESSION_INDEX_LEAF_ID="leaf-a",
    ))

    assert current.to_json_dict() == {
        "session_id": "pi:abc",
        "native_session_id": "abc",
        "source": "pi",
        "source_path": str(tmp_path / "source.jsonl"),
        "transcript_path": str(tmp_path / "pi:abc.md"),
        "tool_log_path": str(tmp_path / "pi:abc.tools.md"),
        "source_path_exists": False,
        "transcript_exists": False,
        "tool_log_exists": False,
        "resolution_method": "session_index_env",
        "leaf_id": "leaf-a",
    }
    json.dumps(current.to_json_dict())


def test_resolve_claude_compat_env_matches_public_contract(tmp_path, monkeypatch):
    monkeypatch.setattr("current_session.transcript.TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("current_session.tool_log.TRANSCRIPT_DIR", str(tmp_path))
    source = tmp_path / "source-transcript.jsonl"
    source.write_text("{}\n")

    compat = resolve_current_session({
        "CLAUDE_SESSION_ID": "claude-compat",
        "CLAUDE_TRANSCRIPT_PATH": str(source),
    })
    public = resolve_current_session(_env(
        session_id="claude-compat",
        native_session_id="claude-compat",
        source="claude",
        source_path=str(source),
    ))

    assert compat.to_json_dict() == public.to_json_dict()
    assert compat.session_id == "claude-compat"
    assert compat.native_session_id == "claude-compat"
    assert compat.source == "claude"
    assert compat.source_path == str(source)
    assert compat.leaf_id is None


def test_resolve_claude_compat_accepts_alternate_transcript_path_env(tmp_path, monkeypatch):
    monkeypatch.setattr("current_session.transcript.TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("current_session.tool_log.TRANSCRIPT_DIR", str(tmp_path))
    source = tmp_path / "claude-alt.jsonl"

    current = resolve_current_session({
        "CLAUDE_SESSION_ID": "claude-alt",
        "CLAUDE_CODE_TRANSCRIPT_PATH": str(source),
    })

    assert current.session_id == "claude-alt"
    assert current.source_path == str(source)
    assert current.transcript_path == str(tmp_path / "claude-alt.md")


def test_insufficient_claude_compat_env_fails_clearly():
    with pytest.raises(CurrentSessionError) as exc:
        resolve_current_session({"CLAUDE_SESSION_ID": "claude-only"})

    message = str(exc.value)
    assert "current only works inside an active agent runtime exposing Session Index env" in message
    assert "insufficient claude compatibility env" in message
    assert "CLAUDE_TRANSCRIPT_PATH" in message


def test_partial_public_env_does_not_fall_back_to_claude_compat(tmp_path):
    with pytest.raises(CurrentSessionError) as exc:
        resolve_current_session({
            "SESSION_INDEX_SESSION_ID": "partial-public",
            "CLAUDE_SESSION_ID": "claude-compat",
            "CLAUDE_TRANSCRIPT_PATH": str(tmp_path / "claude-compat.jsonl"),
        })

    message = str(exc.value)
    assert "missing required env" in message
    assert "SESSION_INDEX_NATIVE_SESSION_ID" in message


def test_missing_required_env_fails_clearly():
    with pytest.raises(CurrentSessionError) as exc:
        resolve_current_session({})

    message = str(exc.value)
    assert "current only works inside an active agent runtime exposing Session Index env" in message
    for key in REQUIRED_ENV_KEYS:
        assert key in message
    for key in CLAUDE_COMPAT_ENV_KEYS[:2]:
        assert key in message


def test_inconsistent_pi_env_fails_clearly(tmp_path):
    with pytest.raises(CurrentSessionError) as exc:
        resolve_current_session(_env(
            session_id="pi:abc",
            native_session_id="def",
            source="pi",
            source_path=str(tmp_path / "source.jsonl"),
        ))

    assert "current only works inside an active agent runtime exposing Session Index env" in str(exc.value)
    assert "inconsistent" in str(exc.value)
