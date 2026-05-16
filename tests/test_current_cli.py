"""CLI tests for the current command."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli


ENV_KEYS = [
    "SESSION_INDEX_SESSION_ID",
    "SESSION_INDEX_NATIVE_SESSION_ID",
    "SESSION_INDEX_SOURCE",
    "SESSION_INDEX_SOURCE_PATH",
    "SESSION_INDEX_LEAF_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_TRANSCRIPT_PATH",
    "CLAUDE_CODE_TRANSCRIPT_PATH",
]


def _clear_current_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _set_pi_env(monkeypatch, source_path, leaf_id="leaf-1"):
    monkeypatch.setenv("SESSION_INDEX_SESSION_ID", "pi:019pi-session")
    monkeypatch.setenv("SESSION_INDEX_NATIVE_SESSION_ID", "019pi-session")
    monkeypatch.setenv("SESSION_INDEX_SOURCE", "pi")
    monkeypatch.setenv("SESSION_INDEX_SOURCE_PATH", str(source_path))
    monkeypatch.setenv("SESSION_INDEX_LEAF_ID", leaf_id)


def _run_cli(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["cli.py", *args])
    cli.main()


def test_current_prints_canonical_id(monkeypatch, tmp_path, capsys):
    _clear_current_env(monkeypatch)
    monkeypatch.setattr("current_session.transcript.TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("current_session.tool_log.TRANSCRIPT_DIR", str(tmp_path))
    _set_pi_env(monkeypatch, tmp_path / "source.jsonl")

    _run_cli(monkeypatch, ["current"])

    assert capsys.readouterr().out == "pi:019pi-session\n"


def test_current_path_prints_clean_transcript_path(monkeypatch, tmp_path, capsys):
    _clear_current_env(monkeypatch)
    monkeypatch.setattr("current_session.transcript.TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("current_session.tool_log.TRANSCRIPT_DIR", str(tmp_path))
    _set_pi_env(monkeypatch, tmp_path / "source.jsonl")

    _run_cli(monkeypatch, ["current", "--path"])

    assert capsys.readouterr().out == f"{tmp_path / 'pi:019pi-session.md'}\n"


def test_current_native_prints_provider_native_id(monkeypatch, tmp_path, capsys):
    _clear_current_env(monkeypatch)
    monkeypatch.setattr("current_session.transcript.TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("current_session.tool_log.TRANSCRIPT_DIR", str(tmp_path))
    _set_pi_env(monkeypatch, tmp_path / "source.jsonl")

    _run_cli(monkeypatch, ["current", "--native"])

    assert capsys.readouterr().out == "019pi-session\n"


def test_current_json_prints_structured_metadata(monkeypatch, tmp_path, capsys):
    _clear_current_env(monkeypatch)
    monkeypatch.setattr("current_session.transcript.TRANSCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr("current_session.tool_log.TRANSCRIPT_DIR", str(tmp_path))
    source = tmp_path / "source.jsonl"
    source.write_text("{}\n")
    transcript_path = tmp_path / "pi:019pi-session.md"
    transcript_path.write_text("transcript")
    _set_pi_env(monkeypatch, source, leaf_id="leaf-json")

    _run_cli(monkeypatch, ["current", "--json"])

    data = json.loads(capsys.readouterr().out)
    assert data == {
        "session_id": "pi:019pi-session",
        "native_session_id": "019pi-session",
        "source": "pi",
        "source_path": str(source),
        "transcript_path": str(transcript_path),
        "tool_log_path": str(tmp_path / "pi:019pi-session.tools.md"),
        "source_path_exists": True,
        "transcript_exists": True,
        "tool_log_exists": False,
        "resolution_method": "session_index_env",
        "leaf_id": "leaf-json",
    }


def test_current_no_env_exits_nonzero_with_clear_error(monkeypatch, capsys):
    _clear_current_env(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, ["current"])

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert captured.out == ""
    assert "current only works inside an active agent runtime exposing Session Index env" in captured.err
    assert "SESSION_INDEX_SESSION_ID" in captured.err
