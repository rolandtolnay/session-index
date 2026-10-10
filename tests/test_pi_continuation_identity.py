"""Public current/snapshot contracts across explicit Pi transfers."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from current_session import CurrentSessionError, resolve_current_session
from indexer import NO_SUMMARY_INDEX_OPTIONS, index_source_transcript
from pi_continuations import record_transfer
from session_identity import canonical_session_id
from snapshot import SnapshotError, capture_snapshot


def write(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return str(path)


def env(path, native, leaf="a"):
    return {
        "SESSION_INDEX_SESSION_ID": canonical_session_id("pi", native),
        "SESSION_INDEX_NATIVE_SESSION_ID": native,
        "SESSION_INDEX_SOURCE": "pi",
        "SESSION_INDEX_SOURCE_PATH": path,
        "SESSION_INDEX_LEAF_ID": leaf,
    }


@pytest.fixture
def transfer_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "data" / "sessions.db"))
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path / "artifacts"))
    main = tmp_path / "main"
    main.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(main), *args], check=True, capture_output=True)
    git("init", "-b", "main")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "initial")
    worktree = tmp_path / "worktree"
    git("worktree", "add", "-b", "feature", str(worktree))
    body = [
        {"type": "message", "id": "u", "parentId": None, "timestamp": "2026-10-01T10:00:00Z", "message": {"role": "user", "content": "Keep the selected solution."}},
        {"type": "message", "id": "a", "parentId": "u", "timestamp": "2026-10-01T10:01:00Z", "message": {"role": "assistant", "content": "The selected solution is safe."}},
        {"type": "message", "id": "abandoned", "parentId": "u", "timestamp": "2026-10-01T10:02:00Z", "message": {"role": "assistant", "content": "Abandoned alternative."}},
        {"type": "session_info", "id": "selected", "parentId": "a", "timestamp": "2026-10-01T10:03:00Z", "name": "selected"},
    ]
    a = write(tmp_path / "a.jsonl", [{"type": "session", "id": "native-a", "cwd": str(main), "timestamp": "2026-10-01T10:00:00Z"}, *body])
    index_source_transcript("pi", a, NO_SUMMARY_INDEX_OPTIONS)
    b = write(tmp_path / "b.jsonl", [{"type": "session", "id": "native-b", "cwd": str(worktree), "parentSession": a, "timestamp": "2026-10-01T10:04:00Z"}, *body])
    return main, worktree, a, b, canonical_session_id("pi", "native-a")


@pytest.fixture
def transferred(transfer_pair):
    _, _, a, b, _ = transfer_pair
    record_transfer(a, b, source_entries=[json.loads(line) for line in Path(a).read_text().splitlines()])
    index_source_transcript("pi", b, NO_SUMMARY_INDEX_OPTIONS)
    return transfer_pair


def test_current_paths_stay_stable_while_snapshot_uses_actual_origin(transferred):
    main, worktree, a, b, owner = transferred
    before = Path(db.DB_PATH).read_bytes()
    current = resolve_current_session(env(b, "native-b"))
    assert current.session_id == owner == canonical_session_id("pi", "native-a")
    assert current.native_session_id == "native-b"
    assert current.source_path == b
    assert Path(current.transcript_path).name == owner + ".md"
    assert Path(current.tool_log_path).name == owner + ".tools.md"
    assert current.transcript_exists
    snapshot = capture_snapshot(cwd=str(worktree), env=env(b, "native-b"))
    assert snapshot["native_session_id"] == "native-b"
    assert snapshot["source_path"] == b and snapshot["leaf_id"] == "a"
    assert "selected solution" in snapshot["transcript"]
    assert "Abandoned" not in snapshot["transcript"]
    indexed = capture_snapshot(cwd=str(worktree), session=owner, leaf_id="a")
    assert indexed["session_id"] == owner
    assert indexed["native_session_id"] == "native-b" and indexed["source_path"] == b
    ancestor = capture_snapshot(cwd=str(main), env=env(a, "native-a"))
    assert ancestor["native_session_id"] == "native-a" and ancestor["source_path"] == a
    assert Path(db.DB_PATH).read_bytes() == before


def test_transferred_identity_does_not_relax_native_worktree_or_leaf_checks(transferred):
    main, worktree, a, b, owner = transferred
    with pytest.raises(CurrentSessionError, match="identity mismatch"):
        resolve_current_session(env(b, "native-a"))
    for overrides, code in [
        ({"cwd": str(main)}, "worktree_mismatch"),
        ({"env": env(b, "native-a")}, "identity_mismatch"),
        ({"env": {k: v for k, v in env(b, "native-b").items() if k != "SESSION_INDEX_LEAF_ID"}}, "missing_leaf"),
    ]:
        args = {"cwd": str(worktree), "env": env(b, "native-b"), **overrides}
        with pytest.raises(SnapshotError) as error:
            capture_snapshot(**args)
        assert error.value.code == code


def test_resolve_only_does_not_open_raw_continuation(transferred, monkeypatch):
    _, worktree, _, b, owner = transferred
    def forbidden(*args, **kwargs):
        pytest.fail("Resolve-only must return the guard target before reading raw data")
    monkeypatch.setattr("snapshot._capture", forbidden)
    monkeypatch.setattr("pi_continuations._raw", forbidden)
    for kwargs in ({"env": env(b, "native-b")}, {"session": owner, "leaf_id": "a"}):
        origin = capture_snapshot(cwd=str(worktree), resolve_only=True, **kwargs)
        assert origin["source_path"] == b and origin["native_session_id"] == "native-b"


def test_workflow_acknowledges_durable_job_and_delayed_ancestor_cannot_regress(transfer_pair, tmp_path, monkeypatch, capsys):
    import io
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
    import pi_index
    import session_refresh
    import _session_refresh_worker as worker
    _, _, a, b, owner = transfer_pair
    monkeypatch.setattr(session_refresh, "REFRESH_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(session_refresh, "_ensure_worker", lambda *_: 0)
    monkeypatch.setattr(pi_index, "log", lambda *_: None)
    monkeypatch.setattr(worker, "log", lambda *_: None)
    monkeypatch.setattr("indexer._summarize_session", lambda *_: None)
    monkeypatch.setattr("summarizer.generate_headline", lambda **_: None)
    monkeypatch.setenv("SESSION_INDEX_NATIVE_SESSION_ID", "stale-process-origin")
    monkeypatch.setattr(sys, "argv", ["pi_index.py", "--mode", "workflow", "--session-file", b,
                                    "--transfer-source-file", a])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        [json.loads(line) for line in Path(a).read_text().splitlines()]
    )))
    pi_index.main()
    assert json.loads(capsys.readouterr().out) == {"session_id": owner, "queued": True}
    pending = list((tmp_path / "jobs").glob("**/pending/*.json"))
    assert len(pending) == 1
    payload = json.loads(pending[0].read_text())
    assert payload["transcript_path"] == b and payload["force_summary"] is False
    # The host can exit now: a new coordinator consumes the durable request.
    assert worker.process_pending_jobs("pi", "native-b", idle_seconds=0)
    session_refresh.enqueue_refresh("pi", "native-a", a)
    assert worker.process_pending_jobs("pi", "native-a", idle_seconds=0)
    with db.get_connection() as conn:
        rows = conn.execute("SELECT session_id, native_session_id, source_path FROM sessions").fetchall()
        assert [tuple(row) for row in rows] == [(owner, "native-b", b)]
    assert not list((tmp_path / "jobs").glob("**/pending/*.json"))
