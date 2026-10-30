"""Backups are only worth having if they restore; exercise rclone crypt against a local remote."""

from datetime import datetime, timedelta
import shutil
import sqlite3

import pytest

from backup import backup, restore, vault_env

pytestmark = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not installed")


def make_index(root, note):
    root.mkdir()
    with sqlite3.connect(root / "sessions.db") as conn:
        conn.execute("pragma journal_mode=wal")
        conn.execute("create table notes (body text)")
        conn.execute("insert into notes values (?)", (note,))
    (root / "transcripts" / "pi:abc").mkdir(parents=True)
    (root / "transcripts" / "pi:abc.md").write_text("secret transcript body")
    (root / "transcripts" / "pi:abc" / "side-chat.md").write_text("side chat")
    (root / "transcripts" / "cc:gone.md").write_text("pruned later")
    (root / "reference-ids.json").write_text('{"cc:short": "cc:full"}')
    (root / "logs").mkdir()
    (root / "logs" / "session-index.log").write_text("not backed up")


def test_round_trip_restores_latest_state_and_keeps_history_bounded(tmp_path):
    source, remote, restored = tmp_path / "source", tmp_path / "remote", tmp_path / "restored"
    make_index(source, "first")
    env = vault_env("correct horse", str(remote))
    first = datetime(2026, 1, 1, 9, 0, 0)

    backup(source, env, now=first)
    (source / "transcripts" / "cc:gone.md").unlink()
    with sqlite3.connect(source / "sessions.db") as conn:
        conn.execute("update notes set body = 'second'")
    backup(source, env, now=first + timedelta(days=100))

    assert [p.name for p in (remote / "db").iterdir()] == ["sessions-20260411-090000.db.gz.bin"]
    assert (remote / "removed" / "20260411-090000" / "transcripts" / "cc:gone.md.bin").exists()
    assert not (remote / "files" / "logs").exists()
    assert all(b"secret transcript" not in p.read_bytes() for p in remote.rglob("*") if p.is_file())

    restore(restored, env)
    with sqlite3.connect(restored / "sessions.db") as conn:
        assert conn.execute("select body from notes").fetchall() == [("second",)]
    assert (restored / "transcripts" / "pi:abc.md").read_text() == "secret transcript body"
    assert (restored / "transcripts" / "pi:abc" / "side-chat.md").read_text() == "side chat"
    assert not (restored / "transcripts" / "cc:gone.md").exists()
    assert (restored / "reference-ids.json").exists()
    assert not (restored / "logs").exists()

    with pytest.raises(SystemExit, match="already exists"):
        restore(restored, env)
    with pytest.raises(Exception):
        restore(tmp_path / "wrong-password", vault_env("wrong", str(remote)))
