"""Tests for Codex Stop hook routing into the shared refresh coordinator."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest


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
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-1"}}) + "\n")
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


def test_lifecycle_routes_interruption_finalization_and_recovery(monkeypatch, tmp_path):
    import session_refresh
    source = tmp_path / "rollout.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread"}}) + "\n")
    queued = []
    recovered = []
    monkeypatch.setattr(codex_stop, "enqueue_refresh", lambda *a, **kw: queued.append(kw) or "job.json")
    monkeypatch.setattr(session_refresh, "launch_recovery", lambda source: recovered.append(source))
    for event in ("Interrupt", "SessionEnd", "SubagentStop", "SessionStart"):
        assert json.loads(_run_hook(monkeypatch, json.dumps({
            "hook_event_name": event, "session_id": "thread", "transcript_path": str(source),
        }))) == {}
    assert [q.get("force_summary", False) for q in queued] == [False, True, False]
    assert recovered == ["codex"]


@pytest.mark.parametrize("recovery_fails", [False, True])
def test_codex_start_injects_saved_context_without_indexing_or_waiting_for_recovery(monkeypatch, tmp_path, recovery_fails):
    import db
    import recent_context
    import session_refresh
    from project_identity import resolve_project

    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(recent_context, "PROJECT_CONTEXT_CONFIG_PATH", str(tmp_path / "missing.json"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    identity = resolve_project(str(cwd))
    transcript = tmp_path / "earlier.md"
    transcript.write_text("Earlier conversation")
    conn = db.get_connection()
    db.init_db(conn)
    for sid, hidden, child in [("visible", False, False), ("hidden", True, False), ("child", False, True)]:
        db.upsert_session(conn, session_id=sid, native_session_id=sid, source="codex",
                          project_id=identity.project_id, project_path=identity.project_path, project=identity.project,
                          headline=f"headline-{sid}", transcript_path=str(transcript), is_subagent=child)
        if hidden:
            conn.execute("UPDATE sessions SET hidden_from_recents=1 WHERE session_id=?", (sid,))
    conn.commit()
    conn.close()
    launched = []
    def launch(source):
        launched.append(source)
        if recovery_fails:
            raise OSError("process launch unavailable")
    monkeypatch.setattr(session_refresh, "launch_recovery", launch)
    def unexpected_index(*args, **kwargs):
        pytest.fail("SessionStart must not enqueue indexing")
    monkeypatch.setattr(codex_stop, "enqueue_refresh", unexpected_index)
    output = json.loads(_run_hook(monkeypatch, json.dumps({
        "hook_event_name": "SessionStart", "source": "resume", "cwd": str(cwd), "session_id": "current",
    })))
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "SessionStart"
    context = specific["additionalContext"]
    assert "headline-visible" in context and str(transcript) in context
    assert "headline-hidden" not in context and "headline-child" not in context
    assert launched == ["codex"]


@pytest.mark.parametrize("corrupt", [False, True])
def test_codex_start_with_missing_or_unreadable_index_returns_empty_json(monkeypatch, tmp_path, corrupt):
    import db
    import session_refresh
    database = tmp_path / "sessions.db"
    if corrupt:
        database.write_text("not sqlite")
    monkeypatch.setattr(db, "DB_PATH", str(database))
    monkeypatch.setattr(session_refresh, "launch_recovery", lambda _source: None)
    output = _run_hook(monkeypatch, json.dumps({"hook_event_name": "SessionStart", "cwd": str(tmp_path)}))
    assert json.loads(output) == {}
