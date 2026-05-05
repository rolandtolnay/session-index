# session-index

Automatic indexing, summarization, and search for Claude Code and Pi conversations.

## What it does

- **Claude Code hooks** — index metadata on Stop, summarize/write transcripts on SessionEnd, inject recent context on SessionStart
- **Pi extension** — indexes Pi sessions after turns/shutdown and injects recent context before the first prompt in a session
- **Unified DB** — stores both sources in `~/.session-index/sessions.db`
- **Clean transcripts** — writes compact markdown transcripts to `~/.session-index/transcripts/`
- **Tool logs** — writes separate per-session tool-call logs to `~/.session-index/transcripts/*.tools.md` when full indexing runs
- **CLI** — search, backfill, status, and excerpts from the terminal
- **Skill** — `session-search` skill for Claude Code and Pi

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

By default the installer sets up both integrations:

- Claude Code: skill symlink in `~/.claude/skills/` and hooks in `~/.claude/settings.json`
- Pi: skill symlink in `~/.pi/agent/skills/` and extension symlink in `~/.pi/agent/extensions/`

Install one target only:

```bash
node install.js --target claude
node install.js --target pi
```

Uninstall:

```bash
node install.js --uninstall
node install.js --uninstall --target pi
```

After installing the Pi integration, run `/reload` in Pi or restart Pi.

## Backfill existing conversations

```bash
uv run cli.py backfill --source all
```

Source-specific backfill:

```bash
uv run cli.py backfill --source claude
uv run cli.py backfill --source pi
```

Progress is per-session and idempotent — safe to interrupt and resume. Pi rows are stored with `pi:<uuid>` DB IDs; raw Pi UUID prefixes also resolve in `excerpt`.

## Search

From Pi, invoke the skill with:

```text
/skill:session-search token refresh
```

From Claude Code, use the installed skill command:

```text
/session-search token refresh
/session-search --project synapto --since 2026-03-01
```

Or from the terminal:

```bash
uv run cli.py search "token refresh"
uv run cli.py search --project session-index --since 2026-03-01
uv run cli.py excerpt 019dde8f -q "pi transcript parser"
uv run cli.py status
```

## Add to global agent instructions

For Claude Code, add to `~/.claude/CLAUDE.md`. For Pi, add to `~/.pi/agent/AGENTS.md` if you want an explicit reminder beyond the installed skill metadata:

```markdown
## Past Conversation Reference

Recent same-project sessions are already in context when session-index is installed.
For anything else — older sessions, other projects, or specific topic lookups — use
the session-search skill. Invoke it proactively when the user references past work,
decisions, or discussions from another project. Do NOT read raw JSONL files.
```

## Important: raw session cleanup

Claude Code may delete JSONL logs after `cleanupPeriodDays` (default: 30 days). Pi session files remain under `~/.pi/agent/sessions/` unless deleted. The session-index DB, cleaned transcripts, and generated tool logs persist independently, so indexed data survives raw-log cleanup/deletion. Cleaned transcripts intentionally omit detailed tool calls; use the `.tools.md` artifact when debugging commands, tool parameters, or returned results.

## CLI Commands

| Command | Description |
|---------|-------------|
| `search [query] [--project NAME] [--since DATE] [--until DATE]` | Full-text search with optional project prefix and date range |
| `excerpt <session>... -q QUERY` | Extract focused transcript passages |
| `backfill [--source claude\|pi\|all] [--force] [--prune] [--project NAME] [--session ID] [--transcripts-only]` | Process JSONL files; `--transcripts-only` skips LLM summaries |
| `status [--fix]` | Index stats + integrity check; `--fix` repairs dangling paths and orphans |

## Data locations

- Database: `~/.session-index/sessions.db`
- Clean transcripts: `~/.session-index/transcripts/{session_id}.md`
- Tool logs: `~/.session-index/transcripts/{session_id}.tools.md`
- Logs: `~/.session-index/logs/session-index.log`
- Claude source JSONL: `~/.claude/projects/{encoded_path}/{session_id}.jsonl`
- Pi source JSONL: `~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl`

## Reset data

To wipe the database and transcripts and start fresh:

```bash
rm ~/.session-index/sessions.db
rm -rf ~/.session-index/transcripts/
```

Then run:

```bash
uv run cli.py backfill --source all
```
