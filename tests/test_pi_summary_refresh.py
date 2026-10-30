"""Pi summary publication follows description inputs, not unrelated raw bytes."""

import json
import sys
from pathlib import Path

import pytest

import db
from summarizer import SummaryResult
from test_pi_continuations import exchange, raw, store, subagent

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import _session_refresh_worker as worker
import session_refresh


@pytest.mark.parametrize("change", ["metadata", "conversation", "child_files"])
def test_forced_refresh_publishes_current_description_inputs(store, monkeypatch, change):
    root, _ = store
    source = raw(root / "parent.jsonl", "parent", exchange("original"))
    child = subagent(source, "worker") if change == "child_files" else None
    raw_before = source.read_bytes()
    calls = []

    def summarize(**inputs):
        calls.append(inputs)
        if len(calls) == 1:
            target = child or source
            if change == "metadata":
                entries = [{"type": "custom", "id": "progress", "parentId": "originalr",
                            "customType": "background-progress", "data": {"state": "completed"}}]
            else:
                entries = exchange("new", "workerr" if child else "originalr")
            with target.open("a") as handle:
                for entry in entries:
                    handle.write(json.dumps(entry) + "\n")
        return SummaryResult(f"summary-{len(calls)}")

    monkeypatch.setattr("summarizer.summarize", summarize)
    monkeypatch.setattr("summarizer.generate_headline", lambda **_: f"headline-{len(calls)}")
    monkeypatch.setattr(session_refresh, "REFRESH_JOBS_DIR", str(root / "jobs"))
    monkeypatch.setattr(session_refresh, "_ensure_worker", lambda *_: 0)
    monkeypatch.setattr(worker, "log", lambda *_: None)
    job = session_refresh.enqueue_refresh("pi", "parent", str(source), force_summary=True)
    assert worker.process_pending_jobs("pi", "parent", idle_seconds=0) is worker.RefreshOutcome.DRAINED

    expected_calls = 1 if change == "metadata" else 2
    assert len(calls) == expected_calls
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM sessions").fetchone()
        assert row["summary"] == f"summary-{expected_calls}"
        assert row["headline"] == f"headline-{expected_calls}"
        assert Path(row["transcript_path"]).is_file()
        if change == "conversation":
            assert row["user_message_count"] == 2
            assert "Request new" in Path(row["transcript_path"]).read_text()
    assert not Path(job).exists()
    if change == "child_files":
        assert source.read_bytes() == raw_before  # Contributor inputs changed without a parent append.
        assert "/tmp/new" not in calls[0]["files_touched"]
        assert "/tmp/new" in calls[-1]["files_touched"]


def test_new_turns_cannot_reset_the_outer_workers_supersession_budget(store, monkeypatch):
    monkeypatch.setenv("SESSION_INDEX_SUMMARY_IDLE_SECONDS", "0")
    root, _ = store
    source = raw(root / "parent.jsonl", "parent", exchange("original"))
    monkeypatch.setattr(session_refresh, "REFRESH_JOBS_DIR", str(root / "jobs"))
    monkeypatch.setattr(session_refresh, "_ensure_worker", lambda *_: 0)
    monkeypatch.setattr(worker, "log", lambda *_: None)
    calls = []

    def summarize(**inputs):
        calls.append(inputs)
        if len(calls) <= 4:
            tag = f"new{len(calls)}"
            parent = "originalr" if len(calls) == 1 else f"new{len(calls)-1}r"
            with source.open("a") as handle:
                for entry in exchange(tag, parent):
                    handle.write(json.dumps(entry) + "\n")
            session_refresh.enqueue_refresh("pi", "parent", str(source), event_id=tag)
        return SummaryResult("current")

    monkeypatch.setattr("summarizer.summarize", summarize)
    monkeypatch.setattr("summarizer.generate_headline", lambda **_: "current")
    session_refresh.enqueue_refresh("pi", "parent", str(source), force_summary=True)
    worker.run_worker("pi", "parent")
    assert len(calls) == 2
    assert list((root / "jobs").glob("**/pending/*.json"))
    assert not list((root / "jobs").glob("**/worker.pid"))


def test_continuously_superseded_summary_retains_work_after_one_retry(store, monkeypatch):
    root, _ = store
    source = raw(root / "parent.jsonl", "parent", exchange("original"))
    calls = []

    def summarize(**inputs):
        calls.append(inputs)
        tag = f"new{len(calls)}"
        parent = "originalr" if len(calls) == 1 else f"new{len(calls)-1}r"
        with source.open("a") as handle:
            for entry in exchange(tag, parent):
                handle.write(json.dumps(entry) + "\n")
        assert len(calls) <= 2, "a changing source must not cause unbounded model calls"
        return SummaryResult("stale")

    monkeypatch.setattr("summarizer.summarize", summarize)
    monkeypatch.setattr("summarizer.generate_headline", lambda **_: "stale")
    monkeypatch.setattr(session_refresh, "REFRESH_JOBS_DIR", str(root / "jobs"))
    monkeypatch.setattr(session_refresh, "_ensure_worker", lambda *_: 0)
    monkeypatch.setattr(worker, "log", lambda *_: None)
    job = session_refresh.enqueue_refresh("pi", "parent", str(source), force_summary=True)
    assert worker.process_pending_jobs("pi", "parent", idle_seconds=0) is worker.RefreshOutcome.SUMMARY_SUPERSEDED
    assert len(calls) == 2 and Path(job).exists()
    with db.get_connection() as conn:
        assert conn.execute("SELECT summary FROM sessions").fetchone()[0] is None

    # A later worker can finish the retained request without another user turn.
    monkeypatch.setattr("summarizer.summarize", lambda **_: SummaryResult("current"))
    monkeypatch.setattr("summarizer.generate_headline", lambda **_: "current")
    assert worker.process_pending_jobs("pi", "parent", idle_seconds=0) is worker.RefreshOutcome.DRAINED
    assert not Path(job).exists()
    with db.get_connection() as conn:
        assert conn.execute("SELECT summary FROM sessions").fetchone()[0] == "current"
