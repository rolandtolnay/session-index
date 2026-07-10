"""Tests for Codex Stop hook queuing and idle-summary coordination."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HOOKS_DIR))

import _codex_index_worker as codex_worker
import codex_stop


def _ignore_log(*_args, **_kwargs):
    return None


def _silence_logs(monkeypatch):
    monkeypatch.setattr(codex_stop, "log", _ignore_log)
    monkeypatch.setattr(codex_worker, "log", _ignore_log)


def _run_hook(monkeypatch, payload: str) -> str:
    _silence_logs(monkeypatch)
    stdin = io.StringIO(payload)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    codex_stop.main()
    return stdout.getvalue()


def _write_job(root: Path, session_id: str, name: str, transcript: Path, stopped_at: float) -> Path:
    pending = root / codex_stop.safe_component(session_id) / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / f"{name}.json"
    path.write_text(json.dumps({
        "session_id": session_id,
        "turn_id": name,
        "transcript_path": str(transcript),
        "stopped_at": stopped_at,
    }) + "\n")
    return path


def test_codex_stop_always_returns_valid_json_on_malformed_input(monkeypatch):
    launched = []
    monkeypatch.setattr(codex_stop, "_launch_worker", launched.append)

    output = _run_hook(monkeypatch, "not json")

    assert json.loads(output) == {}
    assert launched == []


def test_codex_stop_finds_exact_rollout_queues_job_and_launches(monkeypatch, tmp_path):
    session_id = "019f4cee-5ac8-73d3-80db-24b6cce8b52d"
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "2026" / "07" / "10" / f"rollout-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    jobs_root = tmp_path / "jobs"
    launched = []

    monkeypatch.setenv("SESSION_INDEX_CODEX_HOME", str(codex_home))
    monkeypatch.setattr(codex_stop, "CODEX_JOBS_DIR", str(jobs_root))
    monkeypatch.setattr(codex_stop, "_launch_worker", launched.append)

    output = _run_hook(monkeypatch, json.dumps({
        "session_id": session_id,
        "turn_id": "turn-1",
        "transcript_path": None,
        "hook_event_name": "Stop",
    }))

    assert json.loads(output) == {}
    assert launched == [session_id]
    pending = list((jobs_root / session_id / "pending").glob("*.json"))
    assert len(pending) == 1
    payload = json.loads(pending[0].read_text())
    assert payload["transcript_path"] == str(rollout)
    assert payload["turn_id"] == "turn-1"


def test_codex_summary_idle_default_is_five_minutes(monkeypatch):
    monkeypatch.delenv("SESSION_INDEX_CODEX_SUMMARY_IDLE_SECONDS", raising=False)
    assert codex_worker._idle_seconds() == 300.0

    monkeypatch.setenv("SESSION_INDEX_CODEX_SUMMARY_IDLE_SECONDS", "invalid")
    assert codex_worker._idle_seconds() == 300.0


def test_worker_coalesces_pending_stops_into_latest_snapshot_and_one_summary(monkeypatch, tmp_path):
    _silence_logs(monkeypatch)
    session_id = "session-1"
    jobs_root = tmp_path / "jobs"
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(codex_stop, "CODEX_JOBS_DIR", str(jobs_root))
    first = _write_job(jobs_root, session_id, "turn-1", transcript, 10)
    second = _write_job(jobs_root, session_id, "turn-2", transcript, 20)
    deterministic = []
    summaries = []

    codex_worker.process_pending_jobs(
        session_id,
        idle_seconds=60,
        now=lambda: 100,
        sleep=lambda _seconds: None,
        deterministic_index=lambda job: deterministic.append(job.turn_id) or SimpleNamespace(
            skipped_reason="", user_message_count=2,
        ),
        summary_index=lambda job: summaries.append(job.turn_id) or SimpleNamespace(summary_generated=True),
    )

    assert deterministic == ["turn-2"]
    assert summaries == ["turn-2"]
    assert not first.exists()
    assert not second.exists()


def test_worker_refreshes_deterministic_artifacts_and_resets_idle_on_new_stop(monkeypatch, tmp_path):
    _silence_logs(monkeypatch)
    session_id = "session-2"
    jobs_root = tmp_path / "jobs"
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(codex_stop, "CODEX_JOBS_DIR", str(jobs_root))
    _write_job(jobs_root, session_id, "turn-1", transcript, 100)
    clock = [100.0]
    sleeps = [0]
    deterministic = []
    summaries = []

    def fake_sleep(_seconds):
        sleeps[0] += 1
        if sleeps[0] == 1:
            _write_job(jobs_root, session_id, "turn-2", transcript, 150)
            clock[0] = 150
        else:
            clock[0] = 1_000

    codex_worker.process_pending_jobs(
        session_id,
        idle_seconds=60,
        now=lambda: clock[0],
        sleep=fake_sleep,
        deterministic_index=lambda job: deterministic.append(job.turn_id) or SimpleNamespace(
            skipped_reason="", user_message_count=2,
        ),
        summary_index=lambda job: summaries.append(job.turn_id) or SimpleNamespace(summary_generated=True),
    )

    assert deterministic == ["turn-1", "turn-2"]
    assert summaries == ["turn-2"]


def test_worker_consumes_job_after_failed_summary_attempt(monkeypatch, tmp_path):
    _silence_logs(monkeypatch)
    session_id = "session-3"
    jobs_root = tmp_path / "jobs"
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(codex_stop, "CODEX_JOBS_DIR", str(jobs_root))
    job_path = _write_job(jobs_root, session_id, "turn-1", transcript, 10)

    codex_worker.process_pending_jobs(
        session_id,
        idle_seconds=0,
        deterministic_index=lambda _job: SimpleNamespace(skipped_reason="", user_message_count=1),
        summary_index=lambda _job: SimpleNamespace(summary_generated=False),
    )

    assert not job_path.exists()


def test_worker_leaves_job_for_future_retry_after_deterministic_error(monkeypatch, tmp_path):
    _silence_logs(monkeypatch)
    session_id = "session-4"
    jobs_root = tmp_path / "jobs"
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(codex_stop, "CODEX_JOBS_DIR", str(jobs_root))
    job_path = _write_job(jobs_root, session_id, "turn-1", transcript, 10)

    def fail(_job):
        raise RuntimeError("database busy")

    codex_worker.process_pending_jobs(
        session_id,
        idle_seconds=0,
        deterministic_index=fail,
        summary_index=lambda _job: SimpleNamespace(summary_generated=True),
    )

    assert job_path.exists()
