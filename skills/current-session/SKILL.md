---
name: current-session
description: Show the canonical Clean Transcript and Tool Log paths for the exact active Codex conversation. Use when the user invokes $current-session or asks where the current conversation's generated Session Index artifacts are stored.
---

# Current Session

Run the bundled `scripts/current.py` with `uv run --quiet`, resolving the script relative to this skill directory.

Return its two output lines unchanged so the user receives the absolute Clean Transcript and Tool Log paths with `exists` or `missing` status.

If the command fails, report its error. Do not infer the conversation from the newest rollout, database row, or filesystem timestamp.
