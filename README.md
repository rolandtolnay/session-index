# session-index

Automatic indexing, summarization, and search for Claude Code, Pi, and Codex conversations.

## What it does

- **Claude Code hooks** — index metadata on Stop, summarize/write transcripts on SessionEnd, inject recent context on SessionStart
- **Pi extension** — indexes Pi sessions after turns/shutdown and injects recent context before the first prompt in a session
- **Codex backfill** — indexes Codex rollout JSONL transcripts from active and archived session directories
- **Unified DB** — stores all supported sources in `~/.session-index/sessions.db`
- **Clean transcripts** — writes compact markdown transcripts to `~/.session-index/transcripts/`
- **Tool logs** — writes separate per-session tool-call logs to `~/.session-index/transcripts/*.tools.md` when full indexing runs
- **Skill Invocation audits** — normalizes slash commands, Pi skill envelopes, provider Skill tools, and exact `SKILL.md` reads into the canonical `skill_invocations` table
- **CLI** — `find`, `inspect`, `query`, backfill, status, and current-session lookup from the terminal
- **Skill** — `session-search` skill for Claude Code, Pi, and Codex-indexed history

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

By default, backfill regenerates only deterministic artifacts and facts: Clean Transcripts, Tool Logs, Subagent Run transcripts, and structured fact tables. It does not run the LLM summarizer.

```bash
uv run cli.py backfill --source all
```

Source-specific deterministic backfill:

```bash
uv run cli.py backfill --source claude
uv run cli.py backfill --source pi
uv run cli.py backfill --source codex
```

Progress is per-session and idempotent — safe to interrupt and resume. Pi rows are stored with `pi:<uuid>` DB IDs; Codex rows are stored with `codex:<uuid>` DB IDs.

Codex defaults:

```text
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
~/.codex/archived_sessions/rollout-*.jsonl
```

Override those roots when needed:

```bash
uv run cli.py backfill --source codex \
  --codex-session-dir /path/to/sessions \
  --codex-archived-dir /path/to/archived_sessions
```

To force-regenerate deterministic artifacts and fact tables, including historical Skill Invocations:

```bash
uv run cli.py backfill --source all --force
```

For scoped validation, run the same command with `--session SESSION_ID` before a full backfill.

Summary regeneration is opt-in:

```bash
uv run cli.py backfill --source all --with-summary
```

## Evidence retrieval

Use the installed `session-search` skill and CLI `--help` as the canonical LLM operating surface. README stays intentionally brief for adopters/maintainers.

The deterministic workflow is:

1. `query` for counts, rankings, aggregates, and custom SQL; `query --schema` prints a curated LLM-oriented table/reference guide.
2. `find` for compact JSON Evidence Find candidates with Inspection References, summaries, and match metadata — no evidence text or broad artifact inventories.
3. `inspect` for scoped Evidence Inspect packets from selected refs. `inspect --ref session/<id>` works without `--q` to return generated artifact metadata and subagent refs; add `--q` for query-focused Evidence Snippets.

From the terminal:

```bash
uv run cli.py find --topic "token refresh" --limit 5
uv run cli.py find --mutated "etc/prd" --project session-index          # file conversation history by session
uv run cli.py find --mutated "etc/prd" --mutation-mode event            # exact File Mutation rows
uv run cli.py find --skill review --project session-index
uv run cli.py inspect --ref session/pi:abc
uv run cli.py inspect --ref skill/pi:abc/1
uv run cli.py inspect --ref session/pi:abc --q "token refresh"
uv run cli.py inspect --ref tool/pi:abc/12
uv run cli.py query --schema
uv run cli.py status
```

Copy a `ref` or `inspect_refs.primary` value unchanged into `inspect` to retrieve bounded Evidence Packets with artifact metadata, locators, and Evidence Snippets.

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

- `session_id` — Canonical Session ID. Pi sessions use the `pi:<uuid>` namespace, Codex sessions use `codex:<uuid>`, and Claude sessions use the native UUID.
- `native_session_id` — provider-native session ID without Session Index namespacing.
- `source` — provider source, currently `claude`, `pi`, or `codex`.
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
| `SESSION_INDEX_SOURCE` | yes | Provider source (`claude`, `pi`, or `codex`) |
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

Claude Code may delete JSONL logs after `cleanupPeriodDays` (default: 30 days). Pi session files remain under `~/.pi/agent/sessions/` unless deleted. Codex rollout JSONL files live under `~/.codex/sessions/` and `~/.codex/archived_sessions/`; `~/.codex/session_index.jsonl` and `~/.codex/state_5.sqlite` provide metadata but are not the Source Transcript. The session-index DB, cleaned transcripts, and generated tool logs persist independently, so indexed data survives raw-log cleanup/deletion. Cleaned transcripts intentionally omit detailed tool calls; use the `.tools.md` artifact when debugging commands, tool parameters, or returned results.

## CLI Commands

| Command | Description |
|---------|-------------|
| `current [--path\|--native\|--json]` | Show the exact active runtime session from Session Index env |
| `query "SELECT ..." [--json] [--limit N] [--schema]` | Read-only SQL for counts, rankings, aggregates, and custom grouping; `--schema` prints a curated fact-table reference + examples |
| `find [--topic TEXT] [--tool NAME] [--skill NAME] [--mutated PATH] [--subagent NAME] ...` | Compact JSON Evidence Find candidates with Inspection References, summaries, and match metadata; no evidence text or broad artifact inventories |
| `inspect --ref REF [--q TEXT] [--max-snippets N]` | JSON Evidence Packets with generated artifact metadata and scoped Clean Transcript, Tool Log, or Subagent Run Evidence Snippets |
| `backfill [--source claude\|pi\|codex\|all] [--force] [--prune] [--project NAME] [--session ID] [--with-summary]` | Process JSONL files; deterministic artifacts/facts by default; `--with-summary` also regenerates LLM summaries |
| `status [--fix]` | Index stats + integrity check; `--fix` repairs dangling paths and orphans |

`find --mutated` is file conversation history by default: it returns one session-collapsed candidate per Canonical Session ID, with representative matching paths and related tool refs for drill-down. Use `find --mutated PATH --mutation-mode event` for exact File Mutation audit rows. Raw SQL over `file_mutations` remains available for custom aggregates and exact lists, for example: `SELECT DISTINCT path FROM file_mutations WHERE session_id='SESSION_ID' ORDER BY path;`. `files_touched` remains broad search metadata and may include reads/searches.

`find --skill NAME` uses the canonical `skill_invocations` table and returns `skill/<session_id>/<sequence>` refs. SQL audits should aggregate `skill_invocations.skill_name`, not `tool_calls`, because Skill Invocations may originate from slash commands, Pi skill envelopes, provider Skill tools, or exact `SKILL.md` reads.

## Data locations

- Database: `~/.session-index/sessions.db`
- Clean transcripts: `~/.session-index/transcripts/{session_id}.md`
- Tool logs: `~/.session-index/transcripts/{session_id}.tools.md`
- Logs: `~/.session-index/logs/session-index.log`
- Claude source JSONL: `~/.claude/projects/{encoded_path}/{session_id}.jsonl`
- Pi source JSONL: `~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl`
- Codex source JSONL: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- Codex archived source JSONL: `~/.codex/archived_sessions/rollout-*.jsonl`
- Codex metadata: `~/.codex/session_index.jsonl`, `~/.codex/state_5.sqlite`

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
