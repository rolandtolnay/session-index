"""Direct Pi shell history must not make a valid conversation unsnapshotable."""
import json
import subprocess

import pytest

from snapshot import SnapshotError, capture_snapshot


def test_snapshot_tolerates_sdk_non_content_roles_without_including_shell_output(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    entries = [
        {"type": "session", "id": "origin", "cwd": str(repo)},
        {"type": "message", "id": "u", "parentId": None, "message": {"role": "user", "content": "Keep the policy unchanged."}},
        {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": "Only duplicate guards were removed."}},
        {"type": "message", "id": "shell", "parentId": "a", "message": {"role": "bashExecution", "command": "git status", "output": "SHELL_OUTPUT", "exitCode": 0}},
    ]
    path = tmp_path / "source.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    result = capture_snapshot(cwd=str(repo), source="pi", source_path=str(path), native_session_id="origin", leaf_id="shell")
    assert "Keep the policy" in result["transcript"]
    assert "duplicate guards" in result["transcript"]
    assert "SHELL_OUTPUT" not in result["transcript"]
    entries.append({"type": "message", "id": "correction", "parentId": "shell", "message": {"role": "user"}})
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    with pytest.raises(SnapshotError, match="missing/invalid message content"):
        capture_snapshot(cwd=str(repo), source="pi", source_path=str(path), native_session_id="origin", leaf_id="correction")
