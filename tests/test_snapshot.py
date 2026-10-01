"""Public snapshot API/CLI regression coverage for exact, fresh commit origins."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_identity import canonical_session_id
from snapshot import SnapshotError, capture_snapshot

ROOT = Path(__file__).resolve().parents[1]
NATIVE = "origin-123"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    return path


def write_source(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def claude_entries(repo, native=NATIVE):
    return [
        {"type": "user", "sessionId": native, "cwd": str(repo), "message": {"content": "Implement the safe solution."}},
        {"type": "assistant", "sessionId": native, "cwd": str(repo), "message": {"content": [{"type": "text", "text": "The solution preserves the invariant."}]}},
    ]


def pi_entries(repo):
    return [
        {"type": "session", "id": NATIVE, "cwd": str(repo)},
        {"type": "message", "id": "u", "parentId": None, "message": {"role": "user", "content": "Keep the safe solution."}},
        {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Selected solution."},
            {"type": "toolCall", "id": "bash-call", "name": "bash", "arguments": {"command": "private tool arguments"}},
        ]}},
        {"type": "message", "id": "t", "parentId": "a", "message": {"role": "toolResult", "toolCallId": "bash-call", "toolName": "bash", "isError": True, "content": "private tool output"}},
        {"type": "custom", "id": "chat", "parentId": "t", "customType": "side-chat", "data": {"text": "unaccepted Side Chat proposal"}},
        {"type": "message", "id": "abandoned", "parentId": "u", "message": {"role": "assistant", "content": "Abandoned unsafe solution."}},
    ]


def explicit(repo, path, source="claude", **kwargs):
    return capture_snapshot(cwd=str(repo), source=source, source_path=str(path), native_session_id=NATIVE, **kwargs)


def runtime_env(path, source="claude", **extra):
    return {
        "SESSION_INDEX_SESSION_ID": canonical_session_id(source, NATIVE),
        "SESSION_INDEX_NATIVE_SESSION_ID": NATIVE,
        "SESSION_INDEX_SOURCE": source,
        "SESSION_INDEX_SOURCE_PATH": str(path),
        **extra,
    }


def test_fresh_correction_ignores_stale_generated_transcript(repo, tmp_path, monkeypatch):
    path = write_source(tmp_path / "origin.jsonl", claude_entries(repo))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{canonical_session_id('claude', NATIVE)}.md").write_text("STALE intent")
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(cache))
    before_files = sorted(tmp_path.rglob("*"))
    first = capture_snapshot(cwd=str(repo), env=runtime_env(path))
    with path.open("a") as f:
        f.write(json.dumps({"type": "user", "sessionId": NATIVE, "cwd": str(repo), "message": {"content": "Correction: do not change the public API."}}) + "\n")
    second = capture_snapshot(cwd=str(repo), env=runtime_env(path))
    assert "Correction: do not change the public API." in second["transcript"]
    assert "Correction" not in first["transcript"]
    assert "STALE" not in second["transcript"]
    assert first["source_sha256"] != second["source_sha256"]
    assert second["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert second["source_bytes"] == len(path.read_bytes())
    assert second["transcript_sha256"] == hashlib.sha256(second["transcript"].encode("utf-8")).hexdigest()
    assert second["repo_root"] == str(repo.resolve())
    assert second["branch"] == "main"
    assert second["leaf_id"] is None
    assert second["version"] == 1
    assert sorted(tmp_path.rglob("*")) == before_files


def test_pi_selected_non_last_leaf_excludes_abandoned_tools_and_side_chats(repo, tmp_path):
    path = write_source(tmp_path / "pi.jsonl", pi_entries(repo))
    result = explicit(repo, path, "pi", leaf_id="chat")
    assert "Selected solution." in result["transcript"]
    assert "Abandoned" not in result["transcript"]
    assert "private tool" not in result["transcript"]
    assert "unaccepted Side Chat" not in result["transcript"]
    assert "Related artifacts" not in result["transcript"]
    assert result["leaf_id"] == "chat"
    assert result["source_bytes"] == len(path.read_bytes())


@pytest.mark.parametrize("problem", ["unknown", "missing_parent", "cycle", "duplicate", "no_parent"])
def test_broken_pi_leaf_fails(repo, tmp_path, problem):
    entries = pi_entries(repo)
    leaf = "chat"
    if problem == "unknown":
        leaf = "unknown"
    elif problem == "missing_parent":
        entries[2]["parentId"] = "missing"
    elif problem == "cycle":
        entries[1]["parentId"] = "a"
    elif problem == "duplicate":
        entries.append(entries[2])
    else:
        del entries[1]["parentId"]
    path = write_source(tmp_path / "pi.jsonl", entries)
    with pytest.raises(SnapshotError) as error:
        explicit(repo, path, "pi", leaf_id=leaf)
    assert error.value.code == "invalid_source"


def test_missing_pi_leaf_never_guesses_latest(repo, tmp_path):
    path = write_source(tmp_path / "pi.jsonl", pi_entries(repo))
    with pytest.raises(SnapshotError) as error:
        capture_snapshot(cwd=str(repo), env=runtime_env(path, "pi"))
    assert error.value.code == "missing_leaf"


def test_runtime_pi_leaf_and_conflicting_override(repo, tmp_path):
    path = write_source(tmp_path / "pi.jsonl", pi_entries(repo))
    env = runtime_env(path, "pi", SESSION_INDEX_LEAF_ID="a")
    result = capture_snapshot(cwd=str(repo), env=env)
    assert result["leaf_id"] == "a"
    assert "Abandoned" not in result["transcript"]
    with pytest.raises(SnapshotError) as error:
        capture_snapshot(cwd=str(repo), env=env, leaf_id="abandoned")
    assert error.value.code == "invalid_arguments"


@pytest.mark.parametrize("source", ["claude", "pi", "codex"])
def test_wrong_native_identity_fails(repo, tmp_path, source):
    if source == "claude":
        entries = claude_entries(repo, native="wrong")
    elif source == "pi":
        entries = pi_entries(repo)
        entries[0]["id"] = "wrong"
    else:
        entries = [{"type": "session_meta", "payload": {"id": "wrong", "cwd": str(repo)}}]
    path = write_source(tmp_path / "source.jsonl", entries)
    with pytest.raises(SnapshotError) as error:
        explicit(repo, path, source, leaf_id="a" if source == "pi" else None)
    assert error.value.code == "identity_mismatch"


def test_exact_worktree_not_common_repository_or_parser_grouping(repo, tmp_path):
    git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "--allow-empty", "-m", "init")
    worktree = repo / ".claude-worktrees" / "feature"
    git(repo, "worktree", "add", "-b", "feature", str(worktree))
    path = write_source(tmp_path / "source.jsonl", claude_entries(worktree))
    with pytest.raises(SnapshotError) as error:
        explicit(repo, path)
    assert error.value.code == "worktree_mismatch"
    result = explicit(worktree, path)
    assert result["repo_root"] == str(worktree.resolve())
    assert result["branch"] == "feature"


def test_oversized_context_is_not_truncated(repo, tmp_path):
    path = write_source(tmp_path / "source.jsonl", claude_entries(repo))
    with pytest.raises(SnapshotError) as error:
        explicit(repo, path, max_chars=20)
    assert error.value.code == "context_too_large"


@pytest.mark.parametrize("tail", [b'{"type":"user",', b'\xff\n', b'[]\n', b'{"type":"user","message":{}}\n', b'{}', b'{"invalid":NaN}\n', b'{"invalid":"\\ud800"}\n'])
def test_invalid_or_incomplete_source_fails_without_dropping_correction(repo, tmp_path, tail):
    path = write_source(tmp_path / "source.jsonl", claude_entries(repo))
    with path.open("ab") as handle:
        handle.write(tail)
    with pytest.raises(SnapshotError) as error:
        explicit(repo, path)
    assert error.value.code == "invalid_source"


def test_absent_runtime_and_partial_flags_fail(repo):
    with pytest.raises(SnapshotError) as error:
        capture_snapshot(cwd=str(repo), env={})
    assert error.value.code == "invalid_identity"
    with pytest.raises(SnapshotError) as error:
        capture_snapshot(cwd=str(repo), source="pi", env={})
    assert error.value.code == "invalid_arguments"


def test_claude_compatibility_env(repo, tmp_path):
    path = write_source(tmp_path / f"{NATIVE}.jsonl", claude_entries(repo))
    result = capture_snapshot(cwd=str(repo), env={"CLAUDE_CODE_SESSION_ID": NATIVE, "CLAUDE_TRANSCRIPT_PATH": str(path)})
    assert result["session_id"] == canonical_session_id("claude", NATIVE)
    assert "safe solution" in result["transcript"]


def test_codex_uses_captured_rollout_not_external_metadata(repo, tmp_path, monkeypatch):
    path = write_source(tmp_path / "source.jsonl", [
        {"type": "session_meta", "payload": {"id": NATIVE, "cwd": str(repo)}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Preserve the invariant."}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Invariant preserved."}]}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "private tool output"}},
    ])
    def forbidden(*args):
        pytest.fail("Snapshot must not follow Codex metadata")
    monkeypatch.setattr("codex_parser._thread_metadata", forbidden)
    result = explicit(repo, path, "codex")
    assert "Invariant preserved." in result["transcript"]
    assert "private tool" not in result["transcript"]


def test_exact_indexed_identity_is_read_only_and_pi_requires_leaf(repo, tmp_path):
    path = write_source(tmp_path / "pi.jsonl", pi_entries(repo))
    database = tmp_path / "sessions.db"
    sid = canonical_session_id("pi", NATIVE)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sessions (session_id, native_session_id, source, source_path)")
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (sid, NATIVE, "pi", str(path)))
    before = database.read_bytes()
    resolved = capture_snapshot(cwd=str(repo), session=sid, db_path=str(database), leaf_id="a", env={}, resolve_only=True)
    assert resolved == {"session_id": sid, "native_session_id": NATIVE, "source": "pi", "source_path": str(path), "leaf_id": "a"}
    assert database.read_bytes() == before
    with pytest.raises(SnapshotError) as error:
        capture_snapshot(cwd=str(repo), session=sid, db_path=str(database), env={})
    assert error.value.code == "missing_leaf"
    result = capture_snapshot(cwd=str(repo), session=sid, db_path=str(database), leaf_id="a", env={})
    assert "Selected solution." in result["transcript"]
    assert database.read_bytes() == before
    with pytest.raises(SnapshotError) as error:
        capture_snapshot(cwd=str(repo), session="pi:0000000000000000", db_path=str(database))
    assert error.value.code == "unknown_session"
    with pytest.raises(SnapshotError) as error:
        capture_snapshot(cwd=str(repo), session=NATIVE, db_path=str(database))
    assert error.value.code == "invalid_identity"


def test_missing_database_is_not_created(repo, tmp_path):
    database = tmp_path / "missing.db"
    with pytest.raises(SnapshotError):
        capture_snapshot(cwd=str(repo), session=canonical_session_id("claude", NATIVE), db_path=str(database))
    assert not database.exists()


@pytest.mark.parametrize("entrypoint", [[str(ROOT / "cli.py"), "snapshot"], [str(ROOT / "skills/session-search/scripts/snapshot.py")]])
def test_cli_and_wrapper_emit_one_json_on_success_and_failure(repo, tmp_path, entrypoint):
    path = write_source(tmp_path / "source.jsonl", claude_entries(repo))
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("SESSION_INDEX_", "CLAUDE_", "CODEX_"))}
    args = ["--cwd", str(repo), "--source", "claude", "--source-path", str(path), "--native-session-id", NATIVE]
    success = subprocess.run([sys.executable, *entrypoint, *args], capture_output=True, text=True, env=clean_env)
    assert success.returncode == 0, success.stderr + success.stdout
    assert json.loads(success.stdout)["native_session_id"] == NATIVE
    assert not success.stderr
    path.unlink()
    resolved = subprocess.run([sys.executable, *entrypoint, *args, "--resolve-only"], capture_output=True, text=True, env=clean_env)
    assert resolved.returncode == 0, resolved.stderr + resolved.stdout
    assert json.loads(resolved.stdout) == {
        "session_id": canonical_session_id("claude", NATIVE), "native_session_id": NATIVE,
        "source": "claude", "source_path": str(path), "leaf_id": None,
    }
    assert not resolved.stderr
    missing = subprocess.run([sys.executable, *entrypoint, *args], capture_output=True, text=True, env=clean_env)
    assert missing.returncode != 0
    assert json.loads(missing.stdout)["error"]["code"] == "source_unavailable"
    for failure_args in (["--cwd", str(repo)], [], ["--unknown"], ["--cwd", str(repo), "--resolve-only"], ["--cwd", str(repo), "--resolve-only", "--source", "pi"]):
        failure = subprocess.run([sys.executable, *entrypoint, *failure_args], capture_output=True, text=True, env=clean_env)
        assert failure.returncode != 0
        assert set(json.loads(failure.stdout)) == {"error"}
        assert not failure.stderr
