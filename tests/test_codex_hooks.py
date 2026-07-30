"""Tests for Codex Stop hook routing into the shared refresh coordinator."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HOOKS_DIR))

import codex_stop


def _run_hook(monkeypatch, payload: str) -> str:
    monkeypatch.setattr(codex_stop, "log", lambda *_args, **_kwargs: None)
    stdin = io.StringIO(payload)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    codex_stop.main()
    return stdout.getvalue()


def test_codex_stop_always_returns_valid_json_on_malformed_input(monkeypatch):
    queued = []
    monkeypatch.setattr(codex_stop, "enqueue_refresh", lambda *args, **kwargs: queued.append((args, kwargs)))

    output = _run_hook(monkeypatch, "not json")

    assert json.loads(output) == {}
    assert queued == []


def test_codex_stop_finds_exact_rollout_and_queues_shared_refresh(monkeypatch, tmp_path):
    session_id = "019f4cee-5ac8-73d3-80db-24b6cce8b52d"
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "2026" / "07" / "10" / f"rollout-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    queued = []

    monkeypatch.setenv("SESSION_INDEX_CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        codex_stop,
        "enqueue_refresh",
        lambda *args, **kwargs: queued.append((args, kwargs)) or str(tmp_path / "job.json"),
    )

    output = _run_hook(monkeypatch, json.dumps({
        "session_id": session_id,
        "turn_id": "turn-1",
        "transcript_path": None,
        "hook_event_name": "Stop",
    }))

    assert json.loads(output) == {}
    assert queued == [(('codex', session_id, str(rollout)), {"event_id": "turn-1"})]


def test_codex_stop_prefers_valid_supplied_transcript(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    queued = []
    monkeypatch.setattr(
        codex_stop,
        "enqueue_refresh",
        lambda *args, **kwargs: queued.append((args, kwargs)) or str(tmp_path / "job.json"),
    )

    output = _run_hook(monkeypatch, json.dumps({
        "session_id": "thread-1",
        "turn_id": "turn-2",
        "transcript_path": str(rollout),
    }))

    assert json.loads(output) == {}
    assert queued == [(('codex', 'thread-1', str(rollout)), {"event_id": "turn-2"})]
