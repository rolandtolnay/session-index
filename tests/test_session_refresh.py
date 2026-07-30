"""Behavior tests for the shared active-session refresh coordinator."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HOOKS_DIR))

import _session_refresh_worker as worker
import session_refresh


def _result(chars: int, parts, *, summary=False, session_id="session-1"):
    return SimpleNamespace(
        session_id=session_id,
        skipped_reason="",
        summary_generated=summary,
        headline_generated=summary,
        rendered_content_chars=chars,
        rendered_content_signature=tuple(parts),
    )


def _isolate(monkeypatch, tmp_path):
    root = tmp_path / "refresh-jobs"
    monkeypatch.setattr(session_refresh, "REFRESH_JOBS_DIR", str(root))
    monkeypatch.setattr(worker, "log", lambda *_args, **_kwargs: None)
    return root


def _job(monkeypatch, tmp_path, *, source="claude", session_id="session-1", observed=100, force=False, event="e1"):
    _isolate(monkeypatch, tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(session_refresh, "_ensure_worker", lambda *_args: 123)
    path = session_refresh.enqueue_refresh(
        source, session_id, str(transcript), event_id=event,
        observed_at=observed, force_summary=force,
    )
    return Path(path), transcript


def _state(source="claude", session_id="session-1"):
    path = Path(session_refresh.session_job_dir(source, session_id)) / "state.json"
    return json.loads(path.read_text())


def test_defaults_and_codex_legacy_idle_fallback(monkeypatch):
    for name in (
        "SESSION_INDEX_SUMMARY_IDLE_SECONDS",
        "SESSION_INDEX_CODEX_SUMMARY_IDLE_SECONDS",
        "SESSION_INDEX_SUMMARY_CONTENT_CHARS",
        "SESSION_INDEX_SUMMARY_CONTENT_COOLDOWN_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert worker._idle_seconds("claude") == 180
    assert worker._idle_seconds("codex") == 180
    assert worker._content_chars() == 10_000
    assert worker._content_cooldown_seconds() == 60

    monkeypatch.setenv("SESSION_INDEX_CODEX_SUMMARY_IDLE_SECONDS", "27")
    assert worker._idle_seconds("codex") == 27
    assert worker._idle_seconds("pi") == 180
    monkeypatch.setenv("SESSION_INDEX_SUMMARY_IDLE_SECONDS", "12")
    assert worker._idle_seconds("codex") == 12


def test_canonical_ids_and_provider_scoped_safe_directories(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert session_refresh.canonical_session_id("claude", "claude:abc") == "abc"
    assert session_refresh.canonical_session_id("pi", "abc") == "pi:abc"
    assert session_refresh.canonical_session_id("pi", "pi:abc") == "pi:abc"
    assert session_refresh.canonical_session_id("codex", "abc") == "codex:abc"
    assert Path(session_refresh.session_job_dir("pi", "a/b")).parts[-2:] == ("pi", "pi-a-b")
    with pytest.raises(ValueError):
        session_refresh.canonical_session_id("unknown", "abc")


def test_enqueue_writes_atomic_job_and_launches_only_one_live_worker(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    launches = []
    monkeypatch.setattr(session_refresh, "_pid_is_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(session_refresh, "_launch_worker", lambda source, sid: launches.append((source, sid)) or 4321)

    first = session_refresh.enqueue_refresh("pi", "abc", str(transcript), "turn-1", observed_at=10)
    second = session_refresh.enqueue_refresh("pi", "pi:abc", str(transcript), "turn-2", observed_at=20)

    assert launches == [("pi", "pi:abc")]
    assert Path(first).exists() and Path(second).exists()
    payload = json.loads(Path(second).read_text())
    assert payload == {
        "event_id": "turn-2", "force_summary": False, "observed_at": 20.0,
        "session_id": "pi:abc", "source": "pi", "transcript_path": str(transcript),
    }


def test_first_qualifying_snapshot_summarizes_immediately(monkeypatch, tmp_path):
    path, _ = _job(monkeypatch, tmp_path)
    parts = (("user", 40, "u"), ("assistant", 60, "a"))
    summaries = []

    worker.process_pending_jobs(
        "claude", "session-1", idle_seconds=180, now=lambda: 100,
        deterministic_index=lambda _job: _result(100, parts),
        summary_index=lambda job: summaries.append(job.event_id) or _result(100, parts, summary=True),
        has_summary=lambda _sid: False,
    )

    assert summaries == ["e1"]
    assert not path.exists()
    assert _state()["summary_signature"] == [["user", 40, "u"], ["assistant", 60, "a"]]


def test_existing_database_summary_initializes_baseline_without_immediate_summary(monkeypatch, tmp_path):
    path, _ = _job(monkeypatch, tmp_path)
    parts = (("user", 40, "u"), ("assistant", 60, "a"))
    summaries = []

    with pytest.raises(StopIteration):
        worker.process_pending_jobs(
            "claude", "session-1", idle_seconds=1_000, now=lambda: 100,
            sleep=lambda _seconds: (_ for _ in ()).throw(StopIteration),
            deterministic_index=lambda _job: _result(100, parts),
            summary_index=lambda job: summaries.append(job),
            has_summary=lambda _sid: True,
        )

    assert path.exists()
    assert summaries == []
    assert _state()["summary_content_chars"] == 100


def test_content_delta_threshold_summarizes_and_advances_baseline(monkeypatch, tmp_path):
    path, _ = _job(monkeypatch, tmp_path, observed=100)
    baseline = (("user", 100, "u"), ("assistant", 100, "a"))
    state_path = Path(session_refresh.session_job_dir("claude", "session-1")) / "state.json"
    session_refresh._atomic_json(state_path, {
        "summary_signature": [list(part) for part in baseline], "summary_content_chars": 200,
    })
    current = baseline + (("user", 4_000, "u2"), ("assistant", 6_000, "a2"))
    summaries = []

    worker.process_pending_jobs(
        "claude", "session-1", idle_seconds=1_000, content_chars=10_000,
        content_cooldown_seconds=60, now=lambda: 150,
        deterministic_index=lambda _job: _result(10_200, current),
        summary_index=lambda job: summaries.append(job.event_id) or _result(10_200, current, summary=True),
        has_summary=lambda _sid: True,
    )

    assert summaries == ["e1"]
    assert not path.exists()
    assert _state()["summary_content_chars"] == 10_200


def test_content_cooldown_and_exact_signature_prevent_repeat(monkeypatch, tmp_path):
    _job(monkeypatch, tmp_path, observed=200)
    current = (("user", 5_000, "u"), ("assistant", 5_000, "a"))
    state_path = Path(session_refresh.session_job_dir("claude", "session-1")) / "state.json"
    session_refresh._atomic_json(state_path, {
        "summary_signature": [], "summary_content_chars": 0,
        "last_content_attempt_at": 190,
        "last_content_attempt_signature": [list(part) for part in current],
    })
    summaries = []

    with pytest.raises(StopIteration):
        worker.process_pending_jobs(
            "claude", "session-1", idle_seconds=1_000, content_chars=10_000,
            content_cooldown_seconds=60, now=lambda: 300,
            sleep=lambda _seconds: (_ for _ in ()).throw(StopIteration),
            deterministic_index=lambda _job: _result(10_000, current),
            summary_index=lambda job: summaries.append(job), has_summary=lambda _sid: True,
        )
    assert summaries == []


def test_new_job_resets_idle_and_refreshes_deterministic_snapshot(monkeypatch, tmp_path):
    _job(monkeypatch, tmp_path, observed=100)
    clock = [100.0]
    calls = []
    summaries = []

    def sleep(_seconds):
        if clock[0] == 100:
            transcript = tmp_path / "session.jsonl"
            session_refresh.enqueue_refresh("claude", "session-1", str(transcript), "e2", observed_at=150)
            clock[0] = 150
        else:
            clock[0] = 1_000

    worker.process_pending_jobs(
        "claude", "session-1", idle_seconds=60, content_chars=99_999,
        now=lambda: clock[0], sleep=sleep,
        deterministic_index=lambda job: calls.append(job.event_id) or _result(100, (("user", 100, job.event_id),)),
        summary_index=lambda job: summaries.append(job.event_id) or _result(100, (("user", 100, job.event_id),), summary=True),
        has_summary=lambda _sid: True,
    )
    assert calls == ["e1", "e2"]
    assert summaries == ["e2"]


def test_forced_job_summarizes_without_waiting(monkeypatch, tmp_path):
    path, _ = _job(monkeypatch, tmp_path, force=True)
    summaries = []
    worker.process_pending_jobs(
        "claude", "session-1", idle_seconds=10_000, now=lambda: 100,
        deterministic_index=lambda _job: _result(50, (("assistant", 50, "a"),)),
        summary_index=lambda job: summaries.append(job.event_id) or _result(50, (("assistant", 50, "a"),), summary=True),
        has_summary=lambda _sid: True,
    )
    assert summaries == ["e1"]
    assert not path.exists()


def test_failed_initial_attempt_preserves_baseline_and_gets_one_idle_retry(monkeypatch, tmp_path):
    path, _ = _job(monkeypatch, tmp_path, observed=100)
    clock = [100.0]
    parts = (("user", 10, "u"), ("assistant", 20, "a"))
    attempts = []

    def summarize(_job):
        attempts.append(clock[0])
        if len(attempts) == 1:
            return _result(30, parts, summary=False)
        return _result(30, parts, summary=True)

    worker.process_pending_jobs(
        "claude", "session-1", idle_seconds=60, now=lambda: clock[0],
        sleep=lambda _seconds: clock.__setitem__(0, 160),
        deterministic_index=lambda _job: _result(30, parts), summary_index=summarize,
        has_summary=lambda _sid: False,
    )
    assert attempts == [100, 160]
    assert not path.exists()
    assert _state()["summary_content_chars"] == 30


def test_failed_content_attempt_keeps_old_baseline_until_idle_consumes_job(monkeypatch, tmp_path):
    path, _ = _job(monkeypatch, tmp_path, observed=100)
    current = (("user", 10_000, "new"),)
    state_path = Path(session_refresh.session_job_dir("claude", "session-1")) / "state.json"
    session_refresh._atomic_json(state_path, {
        "summary_signature": [["user", 100, "old"]], "summary_content_chars": 100,
    })
    clock = [100.0]
    attempts = []

    worker.process_pending_jobs(
        "claude", "session-1", idle_seconds=60, content_chars=10_000, now=lambda: clock[0],
        sleep=lambda _seconds: clock.__setitem__(0, 160),
        deterministic_index=lambda _job: _result(10_000, current),
        summary_index=lambda _job: attempts.append(clock[0]) or _result(10_000, current, summary=False),
        has_summary=lambda _sid: True,
    )
    assert attempts == [100, 160]
    assert not path.exists()
    assert _state()["summary_content_chars"] == 100


def test_new_turn_refreshes_deterministic_artifacts_while_summary_is_running(monkeypatch, tmp_path):
    _job(monkeypatch, tmp_path, observed=100)
    transcript = tmp_path / "session.jsonl"
    summary_started = threading.Event()
    release_summary = threading.Event()
    second_deterministic = threading.Event()
    summary_exists = [False]
    deterministic_calls = []
    summary_calls = []

    def deterministic(job):
        deterministic_calls.append(job.event_id)
        if job.event_id == "e2":
            second_deterministic.set()
        digest = job.event_id or "empty"
        return _result(100, (("assistant", 100, digest),))

    def summarize(job):
        summary_calls.append(job.event_id)
        if len(summary_calls) == 1:
            summary_started.set()
            assert release_summary.wait(2)
        summary_exists[0] = True
        return _result(100, (("assistant", 100, job.event_id),), summary=True)

    coordinator = threading.Thread(target=lambda: worker.process_pending_jobs(
        "claude", "session-1", idle_seconds=0,
        deterministic_index=deterministic,
        summary_index=summarize,
        has_summary=lambda _sid: summary_exists[0],
    ))
    coordinator.start()
    assert summary_started.wait(2)

    session_refresh.enqueue_refresh(
        "claude", "session-1", str(transcript), "e2", observed_at=110,
    )
    assert second_deterministic.wait(2), "new turn should not wait for the summary call"
    assert not release_summary.is_set()

    release_summary.set()
    coordinator.join(2)
    assert not coordinator.is_alive()
    assert deterministic_calls[:2] == ["e1", "e2"]


def test_deterministic_failure_waits_for_inflight_summary_to_finalize(monkeypatch, tmp_path):
    first_path, _ = _job(monkeypatch, tmp_path, observed=100)
    transcript = tmp_path / "session.jsonl"
    summary_started = threading.Event()
    release_summary = threading.Event()
    deterministic_failed = threading.Event()
    outcome = []

    def deterministic(job):
        if job.event_id == "e2":
            deterministic_failed.set()
            raise RuntimeError("database busy")
        return _result(100, (("assistant", 100, "e1"),))

    def summarize(_job):
        summary_started.set()
        assert release_summary.wait(2)
        return _result(100, (("assistant", 100, "e1"),), summary=True)

    coordinator = threading.Thread(target=lambda: outcome.append(worker.process_pending_jobs(
        "claude", "session-1", idle_seconds=180,
        deterministic_index=deterministic,
        summary_index=summarize,
        has_summary=lambda _sid: False,
    )))
    coordinator.start()
    assert summary_started.wait(2)
    second_path = Path(session_refresh.enqueue_refresh(
        "claude", "session-1", str(transcript), "e2", observed_at=110,
    ))
    assert deterministic_failed.wait(2)
    assert coordinator.is_alive(), "worker ownership must remain until the summary settles"

    release_summary.set()
    coordinator.join(2)
    assert not coordinator.is_alive()
    assert outcome == [False]
    assert not first_path.exists()
    assert second_path.exists(), "failed deterministic work remains for the next event"
    assert _state()["summary_content_chars"] == 100


def test_skipped_snapshot_does_not_consume_job_arriving_while_summary_settles(monkeypatch, tmp_path):
    _job(monkeypatch, tmp_path, observed=100)
    transcript = tmp_path / "session.jsonl"
    summary_started = threading.Event()
    release_summary = threading.Event()
    skipped_seen = threading.Event()
    third_processed = threading.Event()
    summary_exists = [False]

    def deterministic(job):
        if job.event_id == "e2":
            skipped_seen.set()
            return SimpleNamespace(skipped_reason="0 user, 0 assistant msgs")
        if job.event_id == "e3":
            third_processed.set()
        return _result(100, (("assistant", 100, job.event_id),))

    def summarize(job):
        if job.event_id == "e1":
            summary_started.set()
            assert release_summary.wait(2)
        summary_exists[0] = True
        return _result(100, (("assistant", 100, job.event_id),), summary=True)

    coordinator = threading.Thread(target=lambda: worker.process_pending_jobs(
        "claude", "session-1", idle_seconds=0,
        deterministic_index=deterministic,
        summary_index=summarize,
        has_summary=lambda _sid: summary_exists[0],
    ))
    coordinator.start()
    assert summary_started.wait(2)
    session_refresh.enqueue_refresh(
        "claude", "session-1", str(transcript), "e2", observed_at=110,
    )
    assert skipped_seen.wait(2)
    third_path = Path(session_refresh.enqueue_refresh(
        "claude", "session-1", str(transcript), "e3", observed_at=120,
    ))

    release_summary.set()
    assert third_processed.wait(2), "late qualifying work must survive skipped-job cleanup"
    coordinator.join(2)
    assert not coordinator.is_alive()
    assert not third_path.exists(), "the surviving job should be processed normally"


def test_content_attempt_history_blocks_a_b_a_branch_retries(monkeypatch, tmp_path):
    _job(monkeypatch, tmp_path, observed=100)
    baseline = (("user", 100, "old"),)
    branch_a = (("user", 10_000, "a"),)
    branch_b = (("user", 10_000, "b"),)
    state_path = Path(session_refresh.session_job_dir("claude", "session-1")) / "state.json"
    session_refresh._atomic_json(state_path, {
        "summary_signature": [list(part) for part in baseline],
        "summary_content_chars": 100,
        "last_content_attempt_at": 100,
        "content_attempt_keys": [
            worker._signature_key(branch_a),
            worker._signature_key(branch_b),
        ],
    })
    summaries = []

    with pytest.raises(StopIteration):
        worker.process_pending_jobs(
            "claude", "session-1", idle_seconds=10_000, content_chars=10_000,
            content_cooldown_seconds=60, now=lambda: 1_000,
            sleep=lambda _seconds: (_ for _ in ()).throw(StopIteration),
            deterministic_index=lambda _job: _result(10_000, branch_a),
            summary_index=lambda job: summaries.append(job),
            has_summary=lambda _sid: True,
        )

    assert summaries == []


def test_cleanup_keeps_worker_marker_when_late_work_exists(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    job_dir = Path(session_refresh.session_job_dir("claude", "session-1"))
    job_dir.mkdir(parents=True)
    pid_path = job_dir / "worker.pid"
    pid_path.write_text(str(worker.os.getpid()))
    monkeypatch.setattr(worker, "_pending_exists", lambda *_args: True)

    assert worker._cleanup_pid("claude", "session-1") is False
    assert pid_path.exists()


def test_deterministic_failure_retains_job_for_next_event(monkeypatch, tmp_path):
    path, _ = _job(monkeypatch, tmp_path)

    assert worker.process_pending_jobs(
        "claude", "session-1", idle_seconds=0,
        deterministic_index=lambda _job: (_ for _ in ()).throw(RuntimeError("busy")),
        summary_index=lambda _job: None, has_summary=lambda _sid: False,
    ) is False
    assert path.exists()
