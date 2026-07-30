"""Tests for Claude lifecycle hooks routing into the shared coordinator."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HOOKS_DIR))

import session_end
import stop


def _run(monkeypatch, module, payload: dict) -> None:
    monkeypatch.delenv("_CLAUDE_HOOK_NESTED", raising=False)
    monkeypatch.setattr(module, "log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    module.main()


def test_stop_queues_turn_refresh_without_indexing_inline(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    queued = []
    monkeypatch.setattr(
        stop,
        "enqueue_refresh",
        lambda *args, **kwargs: queued.append((args, kwargs)) or str(tmp_path / "job.json"),
    )

    _run(monkeypatch, stop, {
        "session_id": "claude-session",
        "transcript_path": str(transcript),
        "stop_hook_active": False,
    })

    assert queued == [(("claude", "claude-session", str(transcript)), {"event_id": "stop"})]


def test_stop_preserves_nested_and_continuation_guards(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    queued = []
    monkeypatch.setattr(stop, "enqueue_refresh", lambda *args, **kwargs: queued.append((args, kwargs)))

    monkeypatch.setenv("_CLAUDE_HOOK_NESTED", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "nested",
        "transcript_path": str(transcript),
    })))
    stop.main()

    monkeypatch.delenv("_CLAUDE_HOOK_NESTED")
    _run(monkeypatch, stop, {
        "session_id": "continuation",
        "transcript_path": str(transcript),
        "stop_hook_active": True,
    })

    assert queued == []


def test_session_end_queues_forced_final_refresh(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    queued = []
    monkeypatch.setattr(
        session_end,
        "enqueue_refresh",
        lambda *args, **kwargs: queued.append((args, kwargs)) or str(tmp_path / "job.json"),
    )

    _run(monkeypatch, session_end, {
        "session_id": "claude-session",
        "transcript_path": str(transcript),
    })

    assert queued == [(("claude", "claude-session", str(transcript)), {
        "event_id": "session-end",
        "force_summary": True,
    })]
