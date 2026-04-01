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
/session-search --project synapto --since 2026-03-01
/session-search auth flow --project dashboard
```

Or from the terminal:

```bash
uv run cli.py search "token refresh"
uv run cli.py search --project synapto --since 2026-03-01
uv run cli.py status
```

### Add to your global CLAUDE.md

Add this to `~/.claude/CLAUDE.md` so Claude knows to use the index:

```markdown
## Past Conversation Reference

Use `/session-search` to find past conversations across all projects — supports text search,
project prefix filter (`--project`), and date ranges (`--since`, `--until`).
For resumable sessions, results include session IDs for `claude --resume <id>`.
Do NOT read raw JSONL files.
```

## Important: cleanupPeriodDays

Claude Code deletes JSONL logs after `cleanupPeriodDays` (default: 30 days). The session-index DB and transcripts persist independently, so data survives cleanup. But new backfills can only process what's still on disk.

## CLI Commands

| Command | Description |
|---------|-------------|
| `search [query] [--project NAME] [--since DATE] [--until DATE]` | Full-text search with optional project prefix and date range filters |
| `backfill [--force] [--prune] [--project NAME] [--session ID] [--transcripts-only]` | Process JSONL files; `--project` / `--session` to scope, `--transcripts-only` skips LLM summaries |
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
