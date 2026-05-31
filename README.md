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
- [Pi](https://pi.dev) authenticated with a GPT-capable provider (default summaries use `openai-codex/gpt-5.4-mini`)
- Optional fallback: [Ollama](https://ollama.ai) with the configured local model

## Quick start

```bash
git clone https://github.com/rolandtolnay/session-index.git
cd session-index
node install.js
pi          # then run /login and choose a GPT-capable provider such as OpenAI Codex
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

## Summary model configuration

Summaries run in the background through headless Pi print mode. Defaults:

```bash
SESSION_INDEX_SUMMARY_MODEL=openai-codex/gpt-5.4-mini
SESSION_INDEX_SUMMARY_THINKING=low
SESSION_INDEX_SUMMARY_TIMEOUT=180
```

Set `SESSION_INDEX_DISABLE_PI_SUMMARIZER=1` to skip Pi and use the legacy fallback path.

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

## Current session lookup

Inside an active Claude Code or Pi runtime that exposes Session Index environment, the `current` command identifies the exact conversation running that command:

```bash
uv run cli.py current          # Canonical Session ID
uv run cli.py current --path   # deterministic Clean Transcript artifact path; warns if missing
uv run cli.py current --native # provider-native session ID
uv run cli.py current --json   # full current-session metadata
```

In Pi TUI, use `/current-session` to display the active Current Session metadata in a transient, user-only focused display. It is not sent to the model and does not append chat/session history. While the display is focused, `Ctrl+R` explicitly runs Manual Current Session Indexing: the same full Pi indexing pass used on session shutdown for the current snapshot, then refreshes artifact statuses if the display remains open. The CLI remains the terminal/API-oriented interface.

`current --json` uses Session Index terminology:

- `session_id` — Canonical Session ID. Pi sessions use the `pi:<uuid>` namespace; Claude sessions use the native UUID.
- `native_session_id` — provider-native session ID without Session Index namespacing.
- `source` — provider source, currently `claude` or `pi`.
- `source_path` — raw provider Source Transcript path.
- `transcript_path` — generated Clean Transcript Markdown artifact path.
- `tool_log_path` — generated Tool Log Markdown artifact path.
- `source_path_exists`, `transcript_exists`, `tool_log_exists` — whether those paths exist at command time.
- `transcript_written_at`, `tool_log_written_at` — optional UTC filesystem last-written timestamps for generated artifacts when the Clean Transcript or Tool Log file exists; Source Transcript mtimes are not exposed as indexing timestamps.
- `resolution_method` — current resolver, `session_index_env`.
- `leaf_id` — optional Pi leaf metadata when available; it traces the active Pi branch but does not affect session-level artifact paths.

The Session Index-owned runtime environment contract is:

| Variable | Required | Meaning |
|----------|----------|---------|
| `SESSION_INDEX_SESSION_ID` | yes | Canonical Session ID |
| `SESSION_INDEX_NATIVE_SESSION_ID` | yes | Provider-native session ID |
| `SESSION_INDEX_SOURCE` | yes | Provider source (`claude` or `pi`) |
| `SESSION_INDEX_SOURCE_PATH` | yes | Raw provider Source Transcript path |
| `SESSION_INDEX_LEAF_ID` | no | Optional Pi leaf metadata |

`SESSION_INDEX_*` variables are the public contract and take precedence. Claude-native environment can be used only as compatibility input when it provides both the native session ID (`CLAUDE_SESSION_ID`) and Source Transcript path (`CLAUDE_TRANSCRIPT_PATH` or `CLAUDE_CODE_TRANSCRIPT_PATH`) needed to construct the same result.

`current` does not require a database row. It derives `transcript_path` and `tool_log_path` from the Canonical Session ID using the standard artifact paths under `~/.session-index/transcripts/`, so it can work before full indexing has completed. Because those paths can be deterministic before the artifacts are written, `current --path` prints the path on stdout and warns on stderr when the Clean Transcript file does not exist yet; use `current --json` when callers need machine-readable existence flags.

If runtime identity is missing or inconsistent, `current` exits non-zero with a clear error. v1 intentionally does not guess from the latest session, focused terminal, runtime registry, or database. Subagent transcript paths are out of scope for v1; `current` returns only the main session artifact paths.

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
| `current [--path\|--native\|--json]` | Show the exact active runtime session from Session Index env |
| `search [query] [--project NAME] [--since DATE] [--until DATE]` | Full-text search with optional project prefix and date range |
| `excerpt <session>... -q QUERY` | Extract focused transcript passages |
| `query "SELECT ..." [--json] [--limit N] [--schema]` | Read-only SQL over the structured fact tables (`tool_calls`, `subagent_runs`, `question_answers`); `--schema` prints the columns + examples |
| `backfill [--source claude\|pi\|all] [--force] [--prune] [--project NAME] [--session ID] [--no-summary]` | Process JSONL files; `--no-summary` skips the LLM summary (regenerates transcripts, tool logs, and fact tables only) |
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
