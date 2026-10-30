# Store R2 and encryption credentials in the login keychain (once per machine)
backup-setup:
    uv run backup.py setup

# Upload an encrypted backup of the index to Cloudflare R2
backup:
    uv run backup.py run

# Restore the latest backup into an empty data directory
restore dir=(env_var("HOME") / ".session-index"):
    uv run backup.py restore --data-dir "{{dir}}"
