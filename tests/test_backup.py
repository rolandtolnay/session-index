"""Backups are only worth having if they restore; exercise rclone crypt against a local remote."""

from datetime import datetime, timedelta
import os
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


def test_round_trip_restores_latest_state_and_keeps_history_bounded(tmp_path, capsys):
    source, remote, restored = tmp_path / "source", tmp_path / "remote", tmp_path / "restored"
    make_index(source, "first")
    env = vault_env("correct horse", str(remote))
    first = datetime(2026, 1, 1, 9, 0, 0)

    backup(source, env, now=first)
    (source / "transcripts" / "cc:gone.md").unlink()
    with sqlite3.connect(source / "sessions.db") as conn:
        conn.execute("update notes set body = 'second'")
    capsys.readouterr()
    backup(source, env, now=first + timedelta(days=100))

    # Off a terminal, each step prints only its outcome line.
    outcome = capsys.readouterr().out.splitlines()
    assert [line.split("  ")[0] for line in outcome] == ["✓ Database", "✓ Transcripts", "✓ Cleanup"]
    assert "none changed · 1 removed" in outcome[1]
    assert "1 backup older than 90 days removed" in outcome[2]

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


def test_full_backup_catches_changes_the_default_skips(tmp_path):
    source, remote = tmp_path / "source", tmp_path / "remote"
    make_index(source, "note")
    env = vault_env("correct horse", str(remote))
    first = datetime(2026, 1, 1, 9, 0, 0)
    backup(source, env, now=first)

    # Rewritten but timestamped before its last upload, as a timestamp-preserving copy leaves it.
    transcript = source / "transcripts" / "pi:abc.md"
    uploaded_at = transcript.stat().st_mtime
    transcript.write_text("restored older body")
    os.utime(transcript, (uploaded_at - 3600, uploaded_at - 3600))

    def restored_body(name):
        restore(tmp_path / name, env)
        return (tmp_path / name / "transcripts" / "pi:abc.md").read_text()

    backup(source, env, now=first + timedelta(minutes=1))
    assert restored_body("after-default") == "secret transcript body"
    backup(source, env, now=first + timedelta(minutes=2), full=True)
    assert restored_body("after-full") == "restored older body"
