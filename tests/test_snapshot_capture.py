"""Capture cutoff and fail-closed source boundaries at the snapshot interface."""

import builtins
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from snapshot import SnapshotError, capture_snapshot
from test_snapshot import NATIVE, claude_entries, explicit, git, pi_entries, repo, runtime_env, write_source


def test_one_read_initial_byte_cutoff_not_appends_during_capture(repo, tmp_path, monkeypatch):
    path = write_source(tmp_path / "source.jsonl", claude_entries(repo))
    original = path.read_bytes()
    inode = path.stat().st_ino
    fstat = os.fstat
    open_file = builtins.open
    opened = os.open
    source_opens = []
    appended = False

    def append_after_cutoff(fd):
        nonlocal appended
        result = fstat(fd)
        if result.st_ino == inode and not appended:
            appended = True
            with open_file(path, "a") as handle:
                handle.write(json.dumps({"type": "user", "sessionId": NATIVE, "cwd": str(repo), "message": {"content": "Correction after cutoff."}}) + "\n")
        return result

    def track_open(filename, *args, **kwargs):
        if os.fspath(filename) == str(path):
            source_opens.append(filename)
        return opened(filename, *args, **kwargs)

    def no_parser_reread(filename, *args, **kwargs):
        if os.fspath(filename) == str(path):
            pytest.fail("A parser reread the source instead of using captured entries")
        return open_file(filename, *args, **kwargs)

    monkeypatch.setattr("snapshot.os.fstat", append_after_cutoff)
    monkeypatch.setattr("snapshot.os.open", track_open)
    monkeypatch.setattr(builtins, "open", no_parser_reread)
    result = explicit(repo, path)
    assert len(source_opens) == 1
    assert result["source_bytes"] == len(original)
    assert result["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert "Correction after cutoff" not in result["transcript"]


def test_unicode_line_separators_are_text_not_jsonl_boundaries(repo, tmp_path):
    entries = claude_entries(repo)
    entries[0]["message"]["content"] = "Keep café\u2028and preserve the API."
    path = tmp_path / "source.jsonl"
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
    result = explicit(repo, path)
    assert "café\u2028and preserve" in result["transcript"]


@pytest.mark.parametrize("problem", ["missing", "empty", "oversized", "no_cwd"])
def test_source_boundary_failures(repo, tmp_path, monkeypatch, problem):
    entries = claude_entries(repo)
    if problem == "empty":
        entries = entries[:1]
    elif problem == "no_cwd":
        for entry in entries:
            del entry["cwd"]
    path = write_source(tmp_path / "source.jsonl", entries)
    if problem == "missing":
        path.unlink()
    elif problem == "oversized":
        monkeypatch.setattr("snapshot.MAX_SOURCE_BYTES", 10)
    with pytest.raises(SnapshotError) as error:
        explicit(repo, path)
    expected = {"missing": "source_unavailable", "empty": "empty_conversation", "oversized": "source_too_large", "no_cwd": "invalid_worktree"}
    assert error.value.code == expected[problem]


def test_conflicting_session_source_and_inconsistent_runtime_ids_fail(repo, tmp_path):
    path = write_source(tmp_path / "source.jsonl", claude_entries(repo))
    with pytest.raises(SnapshotError) as error:
        explicit(repo, path, session="cc:0000000000000000")
    assert error.value.code == "invalid_arguments"
    env = runtime_env(path)
    env["SESSION_INDEX_SESSION_ID"] = "cc:0000000000000000"
    with pytest.raises(SnapshotError) as error:
        capture_snapshot(cwd=str(repo), env=env)
    assert error.value.code == "invalid_identity"


def test_pi_question_answers_remain_user_intent(repo, tmp_path):
    entries = pi_entries(repo)[:3]
    entries[2]["message"]["content"] = [{"type": "toolCall", "id": "q", "name": "question", "arguments": {"questions": [{"question": "Which API?"}]}}]
    entries.append({"type": "message", "id": "answer", "parentId": "a", "message": {
        "role": "toolResult", "toolCallId": "q", "toolName": "question", "content": "",
        "details": {"selections": [{"question": "Which API?", "selectedOptions": ["Existing API"]}]},
    }})
    # A real assistant reply is required; a tool call alone is not conversation.
    entries.append({"type": "message", "id": "done", "parentId": "answer", "message": {"role": "assistant", "content": "Existing API retained."}})
    path = write_source(tmp_path / "pi.jsonl", entries)
    result = explicit(repo, path, "pi", leaf_id="done")
    assert "[answer] Existing API" in result["transcript"]
    assert "Existing API retained." in result["transcript"]


@pytest.mark.parametrize("source", ["claude", "pi", "codex"])
def test_resolve_only_never_captures_or_inspects_worktree(repo, tmp_path, monkeypatch, source):
    path = tmp_path / (f"{NATIVE}.jsonl" if source == "claude" else "missing.jsonl")

    def forbidden(*args):
        pytest.fail("Resolution must not capture content or inspect the worktree")

    monkeypatch.setattr("snapshot._capture", forbidden)
    monkeypatch.setattr("snapshot._worktree_root", forbidden)
    env = runtime_env(path, source)
    if source == "pi":
        env["SESSION_INDEX_LEAF_ID"] = "leaf-not-verified-until-capture"
    result = capture_snapshot(cwd=str(repo), env=env, resolve_only=True)
    assert result == {
        "session_id": env["SESSION_INDEX_SESSION_ID"], "native_session_id": NATIVE,
        "source": source, "source_path": str(path), "leaf_id": env.get("SESSION_INDEX_LEAF_ID"),
    }
    if source == "claude":
        compat = capture_snapshot(cwd=str(repo), env={"CLAUDE_CODE_SESSION_ID": NATIVE, "CLAUDE_TRANSCRIPT_PATH": str(path)}, resolve_only=True)
        assert compat == result


@pytest.mark.parametrize("arguments,code", [
    ({"env": {}}, "invalid_identity"),
    ({"source": "pi", "env": {}}, "invalid_arguments"),
    ({"source": "pi", "source_path": "/missing.jsonl", "native_session_id": NATIVE}, "missing_leaf"),
    ({"source": "claude", "source_path": "/missing.jsonl", "native_session_id": "cc:bad"}, "invalid_identity"),
    ({"max_chars": 0, "env": {}}, "invalid_arguments"),
    ({"session": "not-canonical", "env": {}}, "invalid_identity"),
])
def test_resolve_only_validates_identity_before_capture(repo, monkeypatch, arguments, code):
    def forbidden(*args):
        pytest.fail("Invalid identity must not reach capture")
    monkeypatch.setattr("snapshot._capture", forbidden)
    with pytest.raises(SnapshotError) as error:
        capture_snapshot(cwd=str(repo), resolve_only=True, **arguments)
    assert error.value.code == code


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_compat_resolution_uses_only_exact_filename_globs(repo, tmp_path, monkeypatch, provider):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    if provider == "claude":
        path = home / ".claude" / "projects" / "encoded-project" / f"{NATIVE}.jsonl"
        env = {"CLAUDE_CODE_SESSION_ID": NATIVE}
    else:
        codex_home = home / ".codex"
        path = codex_home / "archived_sessions" / "2026" / "10" / "01" / f"rollout-time-{NATIVE}.jsonl"
        env = {"CODEX_THREAD_ID": NATIVE, "CODEX_HOME": str(codex_home)}
    path.parent.mkdir(parents=True)
    path.write_text("invalid source content must not be read during resolution")

    def forbidden(*args, **kwargs):
        pytest.fail("Resolution opened source or provider metadata")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr("snapshot.os.open", forbidden)
    result = capture_snapshot(cwd=str(repo), env=env, resolve_only=True)
    assert result["source_path"] == str(path)
    assert result["source"] == provider
    assert result["native_session_id"] == NATIVE
    duplicate = path.parent / "duplicate-directory" / path.name
    if provider == "claude":
        duplicate = path.parent.parent / "another-project" / path.name
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text("another unread source")
    with pytest.raises(SnapshotError) as error:
        capture_snapshot(cwd=str(repo), env=env, resolve_only=True)
    assert error.value.code == "source_unavailable"


def test_capture_rejects_final_symlink_but_resolve_returns_raw_path(repo, tmp_path):
    path = write_source(tmp_path / "source.jsonl", claude_entries(repo))
    link = tmp_path / "linked-source.jsonl"
    link.symlink_to(path)
    resolved = explicit(repo, link, resolve_only=True)
    assert resolved["source_path"] == str(link)
    with pytest.raises(SnapshotError) as error:
        explicit(repo, link)
    assert error.value.code == "source_unavailable"
    assert "safe solution" in explicit(repo, path)["transcript"]
