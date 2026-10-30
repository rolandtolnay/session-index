# Off-site backup and restore

Session Index keeps its data in `~/.session-index/`. Provider logs expire, but the index does not, so for older sessions the index is often the only copy. `backup.py` stores an encrypted copy in a Cloudflare R2 bucket so you can restore it on another machine.

## What is backed up

| Included | Not included |
|----------|--------------|
| `sessions.db` (a consistent snapshot, gzipped) | Raw Claude Code, Pi, and Codex logs |
| `transcripts/` (Clean Transcripts, Tool Logs, Side Chats) | `logs/`, `refresh-jobs/`, lock files |
| `reference-ids.json` | `backups/` and other local scratch files |

Files are encrypted with [rclone crypt](https://rclone.org/crypt/) before upload, so the bucket holds only ciphertext. File names stay readable; they contain only session IDs and dates.

Bucket layout:

```
db/sessions-<timestamp>.db.gz   one database snapshot per run
files/                          mirror of transcripts/ and reference-ids.json
removed/<timestamp>/            files a run deleted or replaced
```

Snapshots and `removed/` folders older than 90 days are deleted at the end of each run, after the new snapshot has been uploaded.

## Requirements

- macOS. Credentials are read from the login keychain with the `security` command.
- [rclone](https://rclone.org), [just](https://github.com/casey/just), and [uv](https://github.com/astral-sh/uv): `brew install rclone just uv`
- A Cloudflare account with R2 enabled. R2 includes a free storage allowance and charges no egress fees; see Cloudflare's pricing for current limits.

## One-time setup

1. In the Cloudflare dashboard, create an R2 bucket named `session-index-backup`.
2. Under **R2 → Manage API tokens**, create a token with **Object Read & Write** permission, applied to that bucket only. The script never needs account-level access.
3. Copy these values from the token page. The secret is shown only once:
   - **Account ID**: 32 hex characters
   - **Access Key ID**: 32 hex characters
   - **Secret Access Key**: 64 hex characters. Do not use the "Token value" field instead.
4. Generate a long random encryption password.
5. Save all four values in your password manager. The encryption password is the only way to read the backup. If it is lost, the backup cannot be recovered.
6. From the repository root, run `just backup-setup` and paste each value when prompted. They are stored in the login keychain under the service `session-index-backup`, with accounts `account-id`, `access-key-id`, `secret-access-key`, and `encryption-password`.

## Backing up

```bash
just backup
```

Run it as often as you like; once or twice a week is plenty for most use. The first run uploads everything. Later runs upload a new database snapshot and only the transcripts that changed. The command reads credentials without prompting, so you can also run it from a scheduler or an agent.

A run prints the snapshot name and size, then confirms the file sync:

```
Database: db/sessions-20260101-090000.db.gz (31.9 MB compressed)
Files: transcripts/ and reference-ids.json synced
```

Transcripts of conversations that are still running keep changing after a backup. The next run picks them up.

## Restoring on a new machine

Restore before you install the hooks. A new session creates an empty `sessions.db`, and the restore refuses to overwrite an existing database.

1. Install the requirements: `brew install rclone just uv`
2. Clone the repository and enter it:
   ```bash
   git clone https://github.com/rolandtolnay/session-index.git
   cd session-index
   ```
3. Run `just backup-setup` and paste the four values from your password manager.
4. Run `just restore`. It downloads the transcripts and `reference-ids.json`, then writes the latest database snapshot to `~/.session-index/sessions.db`. The database is written last, so if it exists, the restore finished.
5. Finish the normal installation from the README's [Quick start](../README.md#quick-start) (`node install.js`, then Pi login).

Restored sessions stay searchable even when their raw provider logs no longer exist. New conversations are indexed as usual once the hooks are installed.

To test a backup without touching your live data, restore into a different directory:

```bash
just restore ~/restore-check
```

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `Missing keychain item session-index-backup/...` | Setup has not been run on this machine. Run `just backup-setup`. |
| `SignatureDoesNotMatch` | Wrong secret access key: it must be the 64-character Secret Access Key, not the token value. Roll the token in the dashboard if the secret is lost. To replace only that value, run `security add-generic-password -U -s session-index-backup -a secret-access-key -w`. |
| `403 Forbidden` / `AccessDenied` | The token does not have Object Read & Write on the `session-index-backup` bucket. |
| `.../sessions.db already exists` | Restore is protecting an existing database. Move it aside, or restore into another directory. |
| Restore fails while decrypting | The encryption password differs from the one used for the backup. |

The round-trip behavior is covered by `tests/test_backup.py`. It uses a local encrypted folder rather than R2 and is skipped when rclone is not installed.
