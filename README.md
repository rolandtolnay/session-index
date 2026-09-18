# session-index

Automatic indexing, summarization, and search for Claude Code, Pi, and Codex conversations.

## What it does

- **Claude Code hooks** — refresh active-session artifacts after turns, force a final refresh on SessionEnd, and inject recent context on SessionStart
- **Pi extension** — refreshes active-session artifacts after turns/shutdown and injects recent context before the first prompt in a session
- **Codex hooks** — refresh active-session artifacts after each Stop (Codex has no distinct session-exit event)
- **Hybrid summary refresh** — summarize the first qualifying turn immediately, then after 180 idle seconds or 10,000 new conversation characters
- **Unified DB** — stores all supported sources in `~/.session-index/sessions.db`
- **Clean transcripts** — writes compact markdown transcripts to `~/.session-index/transcripts/`
- **Tool logs** — writes separate per-session tool-call logs to `~/.session-index/transcripts/*.tools.md` when full indexing runs
- **Skill Invocation audits** — normalizes slash commands, Pi skill envelopes, provider Skill tools, and exact `SKILL.md` reads into the canonical `skill_invocations` table
- **CLI** — `find`, `inspect`, `query`, interactive session management, backfill, status, and current-session lookup from the terminal
- **Skills** — `session-search` for indexed history and Codex `$current-session` for the active conversation's cleaned paths

## Prerequisites

- [Node.js](https://nodejs.org) (for the installer)
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (for running scripts)
- [Pi](https://pi.dev) authenticated with a GPT-capable provider (default summaries use `openai-codex/gpt-5.6-luna`)
- Optional fallback: [Ollama](https://ollama.ai) with the configured local model

## Quick start

```bash
git clone https://github.com/rolandtolnay/session-index.git
cd session-index
node install.js
pi          # then run /login and choose a GPT-capable provider such as OpenAI Codex
```

By default the installer sets up all three integrations:

- Claude Code: skill symlink in `~/.claude/skills/` and hooks in `~/.claude/settings.json`
- Pi: skill symlink in `~/.pi/agent/skills/` and extension symlink in `~/.pi/agent/extensions/`
- Codex: `session-search` and `current-session` skill symlinks in `~/.codex/skills/`, plus the Stop hook in `~/.codex/hooks.json`

Install one target only:

```bash
node install.js --target claude
node install.js --target pi
node install.js --target codex
```

Uninstall:

```bash
node install.js --uninstall
node install.js --uninstall --target pi
node install.js --uninstall --target codex
```

After installing the Pi integration, run `/reload` in Pi or restart Pi.
After installing the Codex integration, restart Codex and use `/hooks` to review and trust the Session Index Stop hook. Codex skips new or changed non-managed hooks until they are trusted.
In Codex, invoke `$current-session` to display the canonical Clean Transcript and Tool Log paths for the active conversation.

## Summary model configuration

Summaries with Substance Bands (`substantial`, `useful`, `low_value`) and separately generated Session Headlines (target 8-15 words, hard maximum 15) run in the background through isolated headless Pi print-mode processes. Defaults:

```bash
SESSION_INDEX_SUMMARY_MODEL=openai-codex/gpt-5.6-luna
SESSION_INDEX_SUMMARY_THINKING=medium
SESSION_INDEX_SUMMARY_TIMEOUT=180
SESSION_INDEX_SUMMARY_IDLE_SECONDS=180
SESSION_INDEX_SUMMARY_CONTENT_CHARS=10000
SESSION_INDEX_SUMMARY_CONTENT_COOLDOWN_SECONDS=60
```

The model/thinking overrides apply to summaries, Substance Bands, and Session Headlines. Set `SESSION_INDEX_DISABLE_PI_SUMMARIZER=1` to skip Pi and use the legacy summary fallback path; headlines and classification require Pi. Failed assessments preserve the last successful band; unassessed sessions are not classified as low-value.

For every supported provider, the first session snapshot with at least one user and one assistant message gets deterministic artifacts plus an immediate summary/headline attempt. Later assistant turns refresh deterministic artifacts immediately. Summary/headline refreshes are coalesced per session and run after either the idle interval or the configured amount of newly rendered user/assistant content; content-trigger attempts observe the cooldown. Claude SessionEnd and Pi shutdown force a final refresh. Codex exposes only turn-level Stop, so its latest snapshot is finalized by the normal idle refresh. `SESSION_INDEX_CODEX_SUMMARY_IDLE_SECONDS` remains a compatibility fallback when the shared idle variable is unset.

## Backfill existing conversations

Hooks/extensions index new Claude, Pi, and Codex conversations automatically. Backfill remains the historical import and repair path.

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

Progress is per-session and idempotent — safe to interrupt and resume. Canonical IDs use `cc:`, `pi:`, or `codex:` followed by 16 hexadecimal hash characters; the full provider ID is retained separately as `native_session_id`.

Codex defaults:

```text
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
~/.codex/archived_sessions/rollout-*.jsonl
```

`SESSION_INDEX_CODEX_HOME` overrides Codex discovery for Session Index-specific testing. Otherwise discovery follows `CODEX_HOME`, then `~/.codex`.

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

Summary regeneration is opt-in. Each successful summary is followed by a separate headline-generation call; sessions are considered complete only when both fields exist:

```bash
uv run cli.py backfill --source all --with-summary
```

Recent context keeps the latest seven current-project sessions. Configured project groups target 14 sessions from the past seven days, ensuring each active group project has a representative even if that exceeds 14. The remaining “Other projects” section contains up to 21 sessions from the same week. Both weekly sections prefer substantial sessions, then useful sessions, newest first within each band. Unknown assessments compete with useful sessions; low-value sessions appear only when needed for group-project coverage.

To populate missing bands for eligible recent sessions without regenerating their summaries or artifacts:

```bash
uv run backfill_substance.py          # preview, no model calls or database updates
uv run backfill_substance.py --apply  # classify existing Clean Transcripts; safe to resume
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

## Manage sessions

Run `uv run cli.py manage` from the repository, or `uv run ~/.pi/agent/skills/session-search/scripts/manage.py` from any directory.

The full-screen terminal browser shows a compact session list beside the selected session’s preview. In narrower terminals, it stacks the list and preview. It uses readable local dates, a visible selection, and color accents. `NO_COLOR` disables colors.

Each page shows up to 20 sessions. Search and filters cover the full inventory, including sessions on other pages. The list scrolls to keep the selected session visible. The header shows the number of results, the active scope, and the sort order.

Press `/` to enter search terms. Press Enter to apply them or Esc to cancel. Every search term must match somewhere in the indexed headlines, summaries, user messages, project names, file paths, or canonical/native session IDs. Partial words match. If the current filters return no exact matches, the search shows deterministic, typo-tolerant near matches. It does not search raw transcripts or assistant messages. The preview shows matching excerpts.

Press `f` to open the filters. You can search for a project or filter by local session-start date, provider, and visibility. Date options include presets and an inclusive custom range. Advanced filters include Substance Band, where Unknown is separate from Low-value. Select Apply filters to use your changes, or press Esc to discard them.

Press `s` to change the sort order. Automatic sorts by newest first while browsing and by best match while searching. You can also choose newest, oldest, best match during search, or substance first. Substance sorting orders sessions by substantial, useful, unknown, and low-value. Within each band, it shows the newest sessions first.

Use these controls:

- ↑/↓ or j/k to select a session.
- ←/→ or p/n to change pages.
- / to search; f for filters; s for sorting.
- c to reset search, filters, and sorting; ? for keyboard help.
- Tab to switch between all and hidden sessions, keeping other filters.
- h to hide or unhide a session.
- d to open the deletion confirmation.
- q to quit.
- PgUp/PgDn to scroll the preview.
- r to refresh the list.

Deletion still requires the full session ID. Esc cancels. Run the browser in an interactive terminal at least 60 columns by 24 rows.

- **Hide from recents** preserves all data and search access. It excludes the session from future recent context for the current project, project group, and other projects. The flag survives indexing refreshes. It does not remove context already injected into an open conversation or prevent deliberate retrieval.
- **Delete** requires typing the full session ID. It removes the database entry, indexed facts, and owned generated artifacts, regardless of the low-value pruning rules. Raw transcripts, shared artifacts, and files outside generated storage remain. There is no undo or deletion exclusion record. Hooks or backfills can re-index preserved raw transcripts. If owned-artifact removal fails, the database entry remains for retry. Files already removed are not restored.

## Current session lookup

Inside an active Claude Code, Pi, or Codex runtime that exposes exact session identity, the `current` command identifies the conversation running that command:

```bash
uv run cli.py current          # Canonical Session ID
uv run cli.py current --path   # deterministic Clean Transcript artifact path; warns if missing
uv run cli.py current --cleaned-paths # Clean Transcript + Tool Log paths with existence status
uv run cli.py current --native # provider-native session ID
uv run cli.py current --json   # full current-session metadata
```

In Pi TUI, use `/current-session` to display the active Current Session metadata in a transient, user-only focused display. It is not sent to the model and does not append chat/session history. While the display is focused, `Ctrl+R` explicitly runs a full indexing pass for the current snapshot, then refreshes artifact statuses if the display remains open. Automatic Pi shutdown queues equivalent final stages through the detached coordinator. The CLI remains the terminal/API-oriented interface.

In Codex, use `$current-session`. The dedicated skill runs the focused `--cleaned-paths` output and returns only the absolute Clean Transcript and Tool Log paths with `[exists]` or `[missing]` status.

`current --json` uses Session Index terminology:

- `session_id` — Canonical Session ID: `cc:<16-hex>`, `pi:<16-hex>`, or `codex:<16-hex>`, derived deterministically from the provider and full native ID.
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

`SESSION_INDEX_*` variables are the public contract and take precedence. Claude-native environment can be used as compatibility input when it identifies one exact Claude source transcript. Codex compatibility uses `CODEX_THREAD_ID` and requires exactly one matching rollout under the active or archived Codex session directories.

`current` does not require a database row. It derives `transcript_path` and `tool_log_path` from the Canonical Session ID using the standard artifact paths under `~/.session-index/transcripts/`, so it can work before full indexing has completed. Because those paths can be deterministic before the artifacts are written, `current --path` prints the path on stdout and warns on stderr when the Clean Transcript file does not exist yet; use `current --json` when callers need machine-readable existence flags.

If runtime identity is missing or inconsistent, `current` exits non-zero with a clear error. v1 intentionally does not guess from the latest session, focused terminal, runtime registry, or database. Subagent transcript paths are out of scope for v1; `current` returns only the main session artifact paths.

## Add to global agent instructions

For Claude Code, add to `~/.claude/CLAUDE.md`. For Pi, add to `~/.pi/agent/AGENTS.md` if you want an explicit reminder beyond the installed skill metadata:

```markdown
## Past Conversation Reference

Recent same-project sessions and selected high-signal cross-project sessions are
already in context when session-index is installed. For anything absent — older
sessions, omitted projects, or specific topic lookups — use the session-search skill.
Invoke it proactively when the user references past work or decisions not listed in
recent context. Do NOT read raw JSONL files.
```

## Important: raw session cleanup

Claude Code may delete JSONL logs after `cleanupPeriodDays` (default: 30 days). Pi session files remain under `~/.pi/agent/sessions/` unless deleted. Codex rollout JSONL files live under `~/.codex/sessions/` and `~/.codex/archived_sessions/`; `~/.codex/session_index.jsonl` and `~/.codex/state_5.sqlite` provide metadata but are not the Source Transcript. The session-index DB, cleaned transcripts, and generated tool logs persist independently, so indexed data survives raw-log cleanup/deletion. Cleaned transcripts intentionally omit detailed tool calls; use the `.tools.md` artifact when debugging commands, tool parameters, or returned results.

## CLI Commands

| Command | Description |
|---------|-------------|
| `manage` | Search, filter, and sort conversations in 20-session pages with previews; confirmed deletion and hide/unhide from recent context |
| `current [--path\|--cleaned-paths\|--native\|--json]` | Show the exact active runtime session or its canonical generated paths |
| `query "SELECT ..." [--json] [--limit N] [--schema]` | Read-only SQL for counts, rankings, aggregates, and custom grouping; `--schema` prints a curated fact-table reference + examples |
| `find [--topic TEXT] [--tool NAME] [--skill NAME] [--mutated PATH] [--subagent NAME] ...` | Compact JSON Evidence Find candidates with Inspection References, summaries, and match metadata; no evidence text or broad artifact inventories |
| `inspect --ref REF [--q TEXT] [--max-snippets N]` | JSON Evidence Packets with generated artifact metadata and scoped Clean Transcript, Tool Log, or Subagent Run Evidence Snippets |
| `backfill [--source claude\|pi\|codex\|all] [--force] [--prune] [--project NAME] [--session ID] [--with-summary]` | Process JSONL files; deterministic artifacts/facts by default; `--with-summary` also regenerates LLM summaries and Session Headlines |
| `status [--fix]` | Index stats + integrity check; `--fix` repairs dangling paths and orphans |

`find --mutated` is file conversation history by default: it returns one session-collapsed candidate per Canonical Session ID, with representative matching paths and related tool refs for drill-down. Use `find --mutated PATH --mutation-mode event` for exact File Mutation audit rows. Raw SQL over `file_mutations` remains available for custom aggregates and exact lists, for example: `SELECT DISTINCT path FROM file_mutations WHERE session_id='SESSION_ID' ORDER BY path;`. `files_touched` remains broad search metadata and may include reads/searches.

`find --skill NAME` uses the canonical `skill_invocations` table and returns `skill/<session_id>/<sequence>` refs. SQL audits should aggregate `skill_invocations.skill_name`, not `tool_calls`, because Skill Invocations may originate from slash commands, Pi skill envelopes, provider Skill tools, or exact `SKILL.md` reads.

## Data locations

- Database: `~/.session-index/sessions.db`
- Clean transcripts: `~/.session-index/transcripts/{session_id}.md`
- Tool logs: `~/.session-index/transcripts/{session_id}.tools.md`
- Logs: `~/.session-index/logs/session-index.log`
- Claude source JSONL: `~/.claude/projects/{encoded_path}/{native_session_id}.jsonl`
- Pi source JSONL: `~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl`
- Codex source JSONL: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- Codex archived source JSONL: `~/.codex/archived_sessions/rollout-*.jsonl`
- Codex metadata: `~/.codex/session_index.jsonl`, `~/.codex/state_5.sqlite`
- Active refresh jobs/state: `~/.session-index/refresh-jobs/{source}/{session-id}/`

## Short-ID migration

Existing UUID-based or 12-hex-ID stores require an offline migration, not a source backfill. Stop agent sessions and detached indexing workers, then run:

```bash
uv run migrate_session_ids.py          # read-only inventory and collision checks
uv run migrate_session_ids.py --apply  # back up, stage, verify, and migrate; resumes interruptions
```

Backups and a complete manifest remain under `~/.session-index/backups/short-ids-*/`. An `identity-migration.json` marker pauses indexing until migration completes; do not remove it during an interrupted migration. Re-run `--apply` to resume. For manual rollback, stop all writers and restore **all four** backup components (`sessions.db`, `transcripts/`, `refresh-jobs/`, `reference-ids.json`) together with the previous code; retain the backup until the cutover is verified.

The migration preserves summaries and facts, renames generated artifacts, and normalizes recognized old artifact paths and Inspection References throughout generated text. Future rendering applies the same normalization. `reference-ids.json` retains the 12-to-16 mapping for generated-text rewriting only; it is not a lookup-alias registry. When upgrading 12-hex IDs, retain the prior migration backups until cutover so native identities for orphan artifacts and quoted references can be recovered. Unrecoverable artifact owners stop the migration; unknown historical references that never resolved remain literal. Native IDs, raw provider files/paths, unrelated UUIDs, logs, and historical backup/report files stay unchanged. No old-path symlinks or legacy canonical-ID lookup aliases are created; provider-native lookup remains available.

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
