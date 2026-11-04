"""Encrypted off-site backup of the Session Index to Cloudflare R2.

Setup:   uv run backup.py setup      (once per machine; prompts for credentials)
Backup:  uv run backup.py run [--full]
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
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

KEYCHAIN_SERVICE = "session-index-backup"
CREDENTIALS = ("account-id", "access-key-id", "secret-access-key", "encryption-password")
BUCKET = "session-index-backup"
RETENTION_DAYS = 90
STAMP_FORMAT = "%Y%m%d-%H%M%S"
FILE_FILTERS = ["--filter", "+ /transcripts/**", "--filter", "+ /reference-ids.json", "--filter", "- *"]
DEFAULT_DATA_DIR = Path.home() / ".session-index"
# Pi theme (claude-code-dark) roles as their nearest xterm-256 colors, as manage_tui.py does.
COLORS = {"accent": 173, "success": 71, "error": 204, "muted": 246}
STATS_FLAGS = ["--use-json-log", "--log-level", "NOTICE", "--stats", "500ms", "--stats-log-level", "NOTICE"]


def duration(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds // 60}m{seconds % 60}s" if seconds >= 60 else f"{seconds}s"


def plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


class Step:
    """One status line per backup step: redrawn in place on a terminal, only the outcome elsewhere."""

    def __init__(self, label: str, stream=None):
        self.label = label
        self.stream = stream or sys.stdout
        self.live = self.stream.isatty()
        self.color = self.live and not os.environ.get("NO_COLOR")
        self.started = time.monotonic()
        self.drawn = False

    def __enter__(self) -> Step:
        self.update()
        return self

    def __exit__(self, kind, error, trace) -> None:
        if kind is not None:
            self.draw("✗", "error", [duration(time.monotonic() - self.started)], final=True)

    def update(self, *details: str) -> None:
        if self.live:
            self.draw("●", "accent", details, final=False)

    def finish(self, *details: str) -> None:
        self.draw("✓", "success", [*details, duration(time.monotonic() - self.started)], final=True)

    def note(self, text: str) -> None:
        """Print a message from rclone above the live line, which the next update redraws."""
        if self.drawn:
            self.stream.write("\r\033[K")
            self.stream.flush()
            self.drawn = False
        sys.stderr.write(text.rstrip() + "\n")

    def paint(self, text: str, role: str) -> str:
        return f"\033[38;5;{COLORS[role]}m{text}\033[0m" if self.color else text

    def draw(self, glyph: str, role: str, details, final: bool) -> None:
        detail = " · ".join(details)
        if self.live:  # A wrapped line cannot be redrawn in place.
            room = shutil.get_terminal_size().columns - len(self.label) - 5
            if len(detail) > room:
                detail = detail[:max(room - 1, 0)] + "…"
        line = f"{self.paint(glyph, role)} {self.label}"
        if detail:
            line += "  " + self.paint(detail, "muted")
        self.stream.write(("\r\033[K" if self.live else "") + line + ("\n" if final else ""))
        self.stream.flush()
        self.drawn = not final


def upload_details(stats: dict) -> list[str]:
    done, total = stats["bytes"], stats["totalBytes"]
    if not total:
        return ["uploading"]
    details = [f"uploading {done / 1e6:.1f} of {total / 1e6:.1f} MB"]
    if stats.get("speed"):
        details.append(f"{stats['speed'] / 1e6:.1f} MB/s")
    if stats.get("eta") and done < total:
        details.append(f"{duration(stats['eta'])} left")
    return details


class SyncDetails:
    """Describe a sync as it runs. rclone's own ETA covers bytes only, while most of a
    run is spent checking unchanged files, so time left comes from the checking rate."""

    def __init__(self):
        self.first: tuple[float, int] | None = None  # (elapsed, checks) when checking began

    def __call__(self, stats: dict) -> list[str]:
        checks, total = stats["checks"], stats["totalChecks"]
        if not total:
            return [f"listing {stats['listed']:,} files"]
        details = [f"{checks:,} of {total:,} checked"]
        if stats["transfers"]:
            details.append(f"{stats['transfers']:,} uploaded")
        if self.first is None:
            self.first = (stats["elapsedTime"], checks)
            return details
        started, checked = self.first
        rate = (checks - checked) / max(stats["elapsedTime"] - started, 1e-6)
        if rate > 0 and checks < total:
            left = max((total - checks) / rate, stats.get("eta") or 0)
            details.append(f"{duration(left)} left")
        return details


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


def rclone(env: dict[str, str], *args: str, step: Step | None = None, describe=None) -> dict:
    """Run rclone; with a step, show its live stats through `describe` and return the final stats."""
    command = ["rclone", "--config", "/dev/null", *args]
    if step is None:
        subprocess.run(command, env=os.environ | env, check=True)
        return {}
    process = subprocess.Popen(command + STATS_FLAGS, env=os.environ | env, stderr=subprocess.PIPE, text=True)
    stats: dict = {}
    for line in process.stderr:
        try:
            entry = json.loads(line)
        except ValueError:
            step.note(line)
            continue
        if "stats" in entry:
            stats = entry["stats"]
            step.update(*describe(stats))
        else:
            step.note(f"rclone {entry.get('level', 'notice')}: {entry.get('msg', '').strip()}")
    if process.wait() != 0:
        raise subprocess.CalledProcessError(process.returncode, command)
    return stats


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


def prune(env: dict[str, str], now: datetime) -> int:
    """Delete snapshots and removed/ folders past retention; return how many runs they came from."""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    expired = set()
    for name in list_names(env, "vault:db"):
        stamp = stamp_of(name)
        if stamp and stamp < cutoff:
            rclone(env, "deletefile", f"vault:db/{name}")
            expired.add(stamp)
    for name in list_names(env, "vault:removed", "--dirs-only"):
        stamp = stamp_of(name)
        if stamp and stamp < cutoff:
            rclone(env, "purge", f"vault:removed/{name.rstrip('/')}")
            expired.add(stamp)
    return len(expired)


def backup(data_dir: Path, env: dict[str, str], now: datetime | None = None, full: bool = False) -> None:
    """Upload a snapshot and sync transcripts.

    By default a transcript is uploaded only when it was modified after its last upload,
    which rclone reads from the bucket listing. `full` instead compares each file's
    recorded modification time, one request per file, and also catches files whose
    content changed without a newer timestamp (restored with an old mtime, or written
    while being uploaded).
    """
    now = now or datetime.now()
    stamp = now.strftime(STAMP_FORMAT)
    with Step("Database") as step, tempfile.TemporaryDirectory() as scratch:
        snapshot = Path(scratch) / f"sessions-{stamp}.db.gz"
        step.update("compressing snapshot")
        snapshot_database(data_dir / "sessions.db", snapshot)
        rclone(env, "copyto", str(snapshot), f"vault:db/{snapshot.name}", step=step, describe=upload_details)
        step.finish(f"db/{snapshot.name}", f"{snapshot.stat().st_size / 1e6:.1f} MB compressed")
    with Step("Transcripts") as step:
        step.update("listing files")
        stats = rclone(
            env, "sync", str(data_dir), "vault:files", *FILE_FILTERS,
            "--backup-dir", f"vault:removed/{stamp}",
            "--fast-list", "--checkers", "32", "--transfers", "16",
            *([] if full else ["--update", "--use-server-modtime"]),
            step=step, describe=SyncDetails(),
        )
        checks, uploads, deletes = (stats.get(key, 0) for key in ("checks", "transfers", "deletes"))
        details = [f"{checks:,} checked"] if checks else []
        details.append(f"{uploads:,} uploaded" if uploads else "none changed")
        if deletes:
            details.append(f"{deletes:,} removed")
        step.finish(*details)
    with Step("Cleanup") as step:
        step.update(f"looking for backups older than {RETENTION_DAYS} days")
        expired = prune(env, now)
        step.finish(f"{plural(expired, 'backup')} older than {RETENTION_DAYS} days removed" if expired
                    else f"nothing older than {RETENTION_DAYS} days")


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
    run_parser = commands.add_parser("run", help="upload an encrypted backup")
    run_parser.add_argument("--full", action="store_true",
                            help="compare every transcript's recorded modification time, not only newer files")
    restore_parser = commands.add_parser("restore", help="restore the latest backup into an empty data directory")
    restore_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()
    if shutil.which("rclone") is None:
        raise SystemExit("rclone is not installed. Run: brew install rclone")
    if args.command == "setup":
        setup()
    elif args.command == "run":
        backup(DEFAULT_DATA_DIR, r2_env(), full=args.full)
    else:
        restore(args.data_dir, r2_env())
    return 0


if __name__ == "__main__":
    sys.exit(main())
