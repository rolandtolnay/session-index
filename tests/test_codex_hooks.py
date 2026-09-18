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


def test_codex_stop_through_shared_worker_preserves_transcript_and_facts(monkeypatch, tmp_path):
    import _session_refresh_worker as worker
    import db
    import session_refresh
    import summarizer
    import tool_log
    import transcript
    from session_identity import canonical_session_id

    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", str(data))
    monkeypatch.setattr(db, "DB_PATH", str(data / "sessions.db"))
    monkeypatch.setattr(session_refresh, "REFRESH_JOBS_DIR", str(data / "refresh-jobs"))
    for module in (transcript, tool_log):
        monkeypatch.setattr(module, "TRANSCRIPT_DIR", str(data / "transcripts"))
    monkeypatch.setenv("SESSION_INDEX_CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(worker, "log", lambda *_args, **_kwargs: None)
    # No background processes or model calls; the queue, coordinator, parser,
    # indexer, SQLite writes, and generated artifacts all run normally.
    monkeypatch.setattr(session_refresh, "_launch_worker", lambda *_args: 12345)
    monkeypatch.setattr(session_refresh, "_pid_is_alive", lambda _pid: False)
    monkeypatch.setattr(summarizer, "summarize", lambda **_kwargs: summarizer.SummaryResult(
        summary="Fixed Codex parsing.", substance_band="useful", substance_reason="Parser repair."))
    monkeypatch.setattr(summarizer, "generate_headline", lambda **_kwargs: "Fixed Codex parsing")

    native_id = "019codex-0000-7000-8000-000000000001"
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes((REPO_ROOT / "tests" / "fixtures" / "codex_sample.jsonl").read_bytes())
    original = rollout.read_bytes()
    output = _run_hook(monkeypatch, json.dumps({
        "session_id": native_id, "turn_id": "turn-1", "transcript_path": str(rollout),
    }))
    assert json.loads(output) == {}
    assert not Path(db.DB_PATH).exists(), "the hook must only enqueue, not index inline"
    assert worker.process_pending_jobs("codex", native_id, idle_seconds=0) is True

    sid = canonical_session_id("codex", native_id)
    conn = db.get_connection()
    try:
        session = db.get_session(conn, sid)
        assert session["source"] == "codex" and session["native_session_id"] == native_id
        assert "Fix the Codex parser in app.py" in Path(session["transcript_path"]).read_text()
        assert Path(session["tool_log_path"]).is_file()
        assert session["summary"] and session["headline"] and session["substance_band"] == "useful"
        assert conn.execute("SELECT path FROM file_mutations WHERE session_id = ?", (sid,)).fetchone()[0] == "/Users/test/project/app.py"
    finally:
        conn.close()
    assert rollout.read_bytes() == original
    assert not list(Path(session_refresh.session_job_dir("codex", sid)).glob("pending/*.json"))
