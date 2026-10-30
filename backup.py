"""Encrypted off-site backup of the Session Index to Cloudflare R2.

Setup:   uv run backup.py setup      (once per machine; prompts for credentials)
Backup:  uv run backup.py run
Restore: uv run backup.py restore [--data-dir DIR]

Backs up what cannot be rebuilt: a consistent snapshot of sessions.db, the
transcripts directory, and reference-ids.json. Everything passes through an
rclone crypt remote, so the bucket only holds ciphertext. Logs, refresh jobs,
locks, and local backups are left out.

Bucket layout (file contents encrypted, names kept plain):
  db/sessions-<stamp>.db.gz      one snapshot per run, kept for RETENTION_DAYS
  files/                         mirror of transcripts/ and reference-ids.json
  removed/<stamp>/               files a run deleted or replaced, kept for RETENTION_DAYS

Credentials live in the login keychain under service "session-index-backup",
where this script reads them without a prompt. Keep the same four values in a
password manager: the encryption password is the only way to read the backup.
See docs/backup.md.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import getpass
import gzip
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

KEYCHAIN_SERVICE = "session-index-backup"
CREDENTIALS = ("account-id", "access-key-id", "secret-access-key", "encryption-password")
BUCKET = "session-index-backup"
RETENTION_DAYS = 90
STAMP_FORMAT = "%Y%m%d-%H%M%S"
FILE_FILTERS = ["--filter", "+ /transcripts/**", "--filter", "+ /reference-ids.json", "--filter", "- *"]
DEFAULT_DATA_DIR = Path.home() / ".session-index"


def read_credentials() -> dict[str, str]:
    values = {}
    for name in CREDENTIALS:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SystemExit(f"Missing keychain item {KEYCHAIN_SERVICE}/{name}. Run: just backup-setup")
        values[name] = result.stdout.strip()
    return values


def setup() -> None:
    print(f"Paste each value from your password manager. Stored in the login keychain as {KEYCHAIN_SERVICE}/<name>.")
    for name in CREDENTIALS:
        value = getpass.getpass(f"{name}: ").strip()
        if not value:
            raise SystemExit(f"{name} is empty; nothing stored.")
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", name, "-w", value],
            check=True,
        )
    print("Stored. Run: just backup")


def vault_env(password: str, remote: str, r2: dict[str, str] | None = None) -> dict[str, str]:
    """rclone remotes defined in the environment: `vault:` encrypts into `remote`."""
    obscured = subprocess.run(
        ["rclone", "obscure", "-"], input=password, capture_output=True, text=True, check=True
    ).stdout.strip()
    env = {
        "RCLONE_CONFIG_VAULT_TYPE": "crypt",
        "RCLONE_CONFIG_VAULT_REMOTE": remote,
        "RCLONE_CONFIG_VAULT_PASSWORD": obscured,
        "RCLONE_CONFIG_VAULT_FILENAME_ENCRYPTION": "off",
        "RCLONE_CONFIG_VAULT_DIRECTORY_NAME_ENCRYPTION": "false",
    }
    if r2:
        env |= {
            "RCLONE_CONFIG_R2_TYPE": "s3",
            "RCLONE_CONFIG_R2_PROVIDER": "Cloudflare",
            "RCLONE_CONFIG_R2_ACCESS_KEY_ID": r2["access-key-id"],
            "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": r2["secret-access-key"],
            "RCLONE_CONFIG_R2_ENDPOINT": f"https://{r2['account-id']}.r2.cloudflarestorage.com",
            # A bucket-scoped token cannot create or list buckets.
            "RCLONE_CONFIG_R2_NO_CHECK_BUCKET": "true",
        }
    return env


def r2_env() -> dict[str, str]:
    credentials = read_credentials()
    return vault_env(credentials["encryption-password"], f"r2:{BUCKET}", credentials)


def rclone(env: dict[str, str], *args: str) -> None:
    subprocess.run(["rclone", "--config", "/dev/null", *args], env=os.environ | env, check=True)


def list_names(env: dict[str, str], path: str, *flags: str) -> list[str]:
    result = subprocess.run(
        ["rclone", "--config", "/dev/null", "lsf", *flags, path],
        env=os.environ | env, capture_output=True, text=True,
    )
    if result.returncode == 3:  # directory not found: nothing stored there yet
        return []
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        result.check_returncode()
    return result.stdout.split()


def snapshot_database(db_path: Path, destination: Path) -> None:
    """Gzip a consistent copy of a live WAL database."""
    copy = destination.with_suffix("")
    source = sqlite3.connect(db_path, timeout=30)
    target = sqlite3.connect(copy)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    with copy.open("rb") as raw, gzip.open(destination, "wb", compresslevel=6) as packed:
        shutil.copyfileobj(raw, packed)
    copy.unlink()


def stamp_of(name: str) -> datetime | None:
    stem = name.rstrip("/").removeprefix("sessions-").removesuffix(".db.gz")
    try:
        return datetime.strptime(stem, STAMP_FORMAT)
    except ValueError:
        return None


def prune(env: dict[str, str], now: datetime) -> None:
    cutoff = now - timedelta(days=RETENTION_DAYS)
    for name in list_names(env, "vault:db"):
        stamp = stamp_of(name)
        if stamp and stamp < cutoff:
            rclone(env, "deletefile", f"vault:db/{name}")
    for name in list_names(env, "vault:removed", "--dirs-only"):
        stamp = stamp_of(name)
        if stamp and stamp < cutoff:
            rclone(env, "purge", f"vault:removed/{name.rstrip('/')}")


def backup(data_dir: Path, env: dict[str, str], now: datetime | None = None) -> None:
    now = now or datetime.now()
    stamp = now.strftime(STAMP_FORMAT)
    with tempfile.TemporaryDirectory() as scratch:
        snapshot = Path(scratch) / f"sessions-{stamp}.db.gz"
        snapshot_database(data_dir / "sessions.db", snapshot)
        rclone(env, "copyto", str(snapshot), f"vault:db/{snapshot.name}")
        print(f"Database: db/{snapshot.name} ({snapshot.stat().st_size / 1e6:.1f} MB compressed)")
    rclone(
        env, "sync", str(data_dir), "vault:files", *FILE_FILTERS,
        "--backup-dir", f"vault:removed/{stamp}",
        "--fast-list", "--checkers", "32", "--transfers", "16",
    )
    print("Files: transcripts/ and reference-ids.json synced")
    prune(env, now)


def restore(data_dir: Path, env: dict[str, str]) -> None:
    db_path = data_dir / "sessions.db"
    if db_path.exists():
        raise SystemExit(f"{db_path} already exists. Move it aside, then rerun the restore.")
    snapshots = sorted(n for n in list_names(env, "vault:db") if stamp_of(n))
    if not snapshots:
        raise SystemExit("No database snapshot in the backup.")
    data_dir.mkdir(parents=True, exist_ok=True)
    rclone(env, "copy", "vault:files", str(data_dir), "--fast-list", "--transfers", "16")
    with tempfile.TemporaryDirectory() as scratch:
        packed = Path(scratch) / snapshots[-1]
        rclone(env, "copyto", f"vault:db/{snapshots[-1]}", str(packed))
        partial = data_dir / "sessions.db.partial"
        with gzip.open(packed, "rb") as source, partial.open("wb") as target:
            shutil.copyfileobj(source, target)
        # The database appears last, so its presence marks a complete restore.
        partial.rename(db_path)
    print(f"Restored {snapshots[-1]} and transcripts into {data_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="store credentials in the login keychain")
    commands.add_parser("run", help="upload an encrypted backup")
    restore_parser = commands.add_parser("restore", help="restore the latest backup into an empty data directory")
    restore_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()
    if shutil.which("rclone") is None:
        raise SystemExit("rclone is not installed. Run: brew install rclone")
    if args.command == "setup":
        setup()
    elif args.command == "run":
        backup(DEFAULT_DATA_DIR, r2_env())
    else:
        restore(args.data_dir, r2_env())
    return 0


if __name__ == "__main__":
    sys.exit(main())
