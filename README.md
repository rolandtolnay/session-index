# session-index

Automatic indexing, summarization, and search for Claude Code conversations.

## What it does

- **Stop hook** — indexes session metadata (files, tools, messages) on every conversation pause
- **SessionEnd hook** — generates an LLM summary and cleaned transcript when a session ends
- **SessionStart hook** — injects recent session context into new conversations
- **CLI** — search, backfill, and status from the terminal
- **Skill** — `/session-search` slash command for searching from any conversation

## Prerequisites

- [Node.js](https://nodejs.org) (for the installer)
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (for running scripts)
- [Ollama](https://ollama.ai) with `qwen3.5:4b` model (for summaries)

## Quick start

```bash
git clone https://github.com/rolandtolnay/session-index.git
cd session-index
node install.js
ollama pull qwen3.5:4b
```

The installer symlinks the search skill into `~/.claude/skills/` and registers the hooks in `~/.claude/settings.json`. Run `node install.js --uninstall` to cleanly reverse everything.

### Backfill existing conversations

```bash
uv run cli.py backfill
```

Progress is per-session and idempotent — safe to interrupt and resume.

### Search

From any Claude Code conversation:

```
/session-search token refresh
```

Or from the terminal:

```bash
uv run cli.py search "token refresh"
uv run cli.py status
```

## Important: cleanupPeriodDays

Claude Code deletes JSONL logs after `cleanupPeriodDays` (default: 30 days). The session-index DB and transcripts persist independently, so data survives cleanup. But new backfills can only process what's still on disk.

## CLI Commands

| Command | Description |
|---------|-------------|
| `search "query"` | Full-text search across messages, summaries, files, projects |
| `backfill [--force] [--prune]` | Process all JSONL files; `--prune` removes noise sessions first |
| `status [--fix]` | Index stats + integrity check; `--fix` repairs dangling paths and orphans |

## Reset data

To wipe the database and transcripts and start fresh:

```bash
rm ~/.session-index/sessions.db
rm -rf ~/.session-index/transcripts/
```

Then run `uv run cli.py backfill` to rebuild from your JSONL logs.

## Uninstall

```bash
node install.js --uninstall
```
