# Debugging Guide

Architecture details, log format, and troubleshooting for session-index.

---

## Architecture Overview

```
Claude settings.json hooks config
    │
    ├─ SessionStart ──────► session_start.py ──► inject recent context into conversation
    │
    ├─ Stop ──────────────► stop.py ──► shared indexer fast upsert
    │
    └─ SessionEnd ────────► session_end.py ──► fork _session_end_worker.py
                                                 └─ shared indexer full pass

Pi extension
    │
    ├─ before_agent_start ─► pi_context.py ──► inject recent context into system prompt
    ├─ agent_end ──────────► pi_index.py --mode fast
    ├─ /current-session Ctrl+R ─► pi_index.py --mode full for the focused current snapshot
    └─ session_shutdown ───► pi_index.py --mode full

Shared full pass:
    parser adapter ─► rich transcript render ─► LLM summary via headless Pi ─► Clean Transcript + Tool Log ─► DB upsert + fact tables
                                                                                     └─► Skill Invocations from slash commands, skill envelopes, Skill tools, and exact SKILL.md reads

Canonical evidence path:
    find/query ──► Inspection Reference ──► inspect ──► artifact metadata + scoped Evidence Snippets

Current-session lookup:
    active runtime env ──► current_session.py ──► cli.py current ──► Canonical Session ID / generated artifact paths
                         ├─► optional generated-artifact last-written timestamps
                         └─► no DB, latest-session, terminal, or registry fallback
```

## File Map

| File | Purpose |
|------|---------|
| `hooks/session_start.py` | Claude SessionStart: injects recent same-project + cross-project context |
| `hooks/stop.py` | Claude Stop: shared deterministic fast upsert |
| `hooks/session_end.py` | Claude SessionEnd: launches detached worker |
| `hooks/_session_end_worker.py` | Claude detached worker: runs shared full index pass |
| `hooks/pi_index.py` | Pi extension entry point for fast/full indexing |
| `hooks/pi_context.py` | Pi extension entry point for recent-context system prompt injection |
| `pi-extension/index.ts` | Pi extension wiring for lifecycle events |
| `pi-extension/session-index-env.ts` | Pi runtime environment helper for current-session lookup |
| `current_session.py` | Exact current-session resolver using Session Index runtime env |
| `indexer.py` | Shared parse/summarize/transcript/upsert pipeline |
| `sources.py` | Claude/Pi Source Transcript discovery for backfill |
| `recent_context.py` | Shared recent-session context builder |
| `cli.py` | CLI entry point: current, find, inspect, query, backfill, status |
| `db.py` | SQLite operations: provider-aware schema, FTS-backed candidate lookup, read-only query helpers, stats |
| `evidence_find.py` | Evidence Find candidate retrieval and JSON construction |
| `evidence_inspect.py` | Evidence Inspect reference resolution and packet construction |
| `inspect_refs.py` | Inspection Reference parsing/formatting |
| `transcript.py` | Clean Transcript writer + Evidence Snippet selector |
| `tool_log.py` | Per-session Markdown Tool Log writer and section extractor |
| `skill_facts.py` | Canonical Skill Invocation extraction and row building |
| `summarizer.py` | LLM summary generator using headless Pi, with legacy Gemini/Ollama fallback |
| `logger.py` | Structured logging with monthly rotation |
| `client.py` | Standalone Ollama HTTP client for fallback summaries (pure stdlib) |
| `skills/session-search/SKILL.md` | Canonical installed LLM operating guide |
| `skills/session-search/scripts/*.py` | Thin wrappers for current, find, inspect, and query |

---

## Data Locations

| Data | Path | Lifetime |
|------|------|----------|
| Database | `~/.session-index/sessions.db` | Permanent |
| Clean Transcripts | `~/.session-index/transcripts/{session_id}.md` | Permanent |
| Tool Logs | `~/.session-index/transcripts/{session_id}.tools.md` | Permanent |
| Subagent Run transcripts | `~/.session-index/transcripts/{session_id}/agent-*.md` | Permanent |
| Log (current month) | `~/.session-index/logs/session-index.log` | Monthly rotation |
| Log (previous month) | `~/.session-index/logs/session-index.prev.log` | Overwritten monthly |
| Claude Source Transcript | `~/.claude/projects/{encoded_path}/{session_id}.jsonl` | Claude Code managed |
| Pi Source Transcript | `~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl` | Pi managed |

---

## Debugging

### Log File

Location: `~/.session-index/logs/session-index.log`. Rotates monthly — current + previous month retained.

All hooks and query calls log their code paths. If a hook fires and there is no log line at all, the hook runner itself likely failed before the script executed.

### Log Format

```
HH:MM:SS.mmm [sid] hook_name          | message
```

- `HH:MM:SS.mmm` — wall-clock timestamp with millisecond precision
- `[sid]` — last 6 characters of the session ID, or `??????` if unavailable
- `hook_name` — left-padded to 18 chars. Common values: `session_start`, `session_end`, `worker`, `stop`, `pi_index`, `pi_context`, `query`
- `message` — free-form, action-oriented

### Filtering by Session

```bash
grep '\[abc123\]' ~/.session-index/logs/session-index.log
```

The `[sid]` tag links all activity for a session: hook events, worker progress, and CLI query calls made during that conversation.

### Example: Full Session Lifecycle

```
15:30:01.100 [a1b2c3] session_start      | started
15:30:01.130 [a1b2c3] session_start      | injected 3 same + 8 cross
15:32:45.200 [a1b2c3] stop               | started
15:32:45.230 [a1b2c3] stop               | upserted (4 msgs, 2 files)
15:35:10.400 [a1b2c3] stop               | started
15:35:10.430 [a1b2c3] stop               | upserted (6 msgs, 3 files)
15:40:00.100 [a1b2c3] session_end        | launching worker
15:40:00.110 [a1b2c3] session_end        | worker launched
15:40:00.200 [a1b2c3] worker             | started
15:40:03.500 [a1b2c3] worker             | llm summary generated (3.2s)
15:40:03.600 [a1b2c3] worker             | transcript written
15:40:03.650 [a1b2c3] worker             | upserted final
```

---

## Diagnosing Common Issues

**Session not indexed:**
- `stop | skipped (N user, M assistant msgs)` or `pi_index | fast skipped (...)` — needs at least 1 user + 1 assistant message
- Claude: no `session_end` or `worker` lines — session still active, or SessionEnd hook did not fire
- Pi: no `pi_index` lines — run `/reload` or restart Pi after installing the extension
- `worker | jsonl not found` / `pi_index | missing session file` — Source Transcript path mismatch

**Summary missing:**
- Check Pi auth/model availability: default is `openai-codex/gpt-5.4-mini` via `pi -p --no-session --no-tools`.
- Set `SESSION_INDEX_SUMMARY_MODEL`, `SESSION_INDEX_SUMMARY_THINKING`, or `SESSION_INDEX_SUMMARY_TIMEOUT` to override the default.
- Set `SESSION_INDEX_DISABLE_PI_SUMMARIZER=1` to force the legacy fallback path.
- Run `uv run cli.py status` to find sessions missing summaries.

**Evidence Find returns no candidates:**
- Use `uv run cli.py find --help` to confirm criteria. `find` requires at least one criterion/filter.
- FTS5 tokenization splits underscores and punctuation. Try fewer words or separated terms, e.g. `cooldown seconds`.
- Project filter is prefix match: `--project ghostty` matches `ghostty-peon`.
- Date filters are inclusive for bare dates.
- For exact File Mutation trails or aggregates, use `query --schema` then SQL over `file_mutations`.
- For skill audits, `find --skill NAME` and SQL should use `skill_invocations`; `tool_calls` intentionally has no `skill_name` column.

**Skill Invocation rows are stale or missing:**
- Confirm the deterministic facts exist: `uv run cli.py query "SELECT skill_name, COUNT(*) AS n FROM skill_invocations GROUP BY skill_name ORDER BY n DESC LIMIT 20" --json`.
- Regenerate one known session first: `uv run cli.py backfill --source all --session SESSION_ID --force`.
- If scoped repair works, run the full deterministic repair: `uv run cli.py backfill --source all --force`.
- Historical repair is a deterministic reindex/backfill, not a transcript-only migration, because Skill Invocations depend on parser metadata, combined Tool Call sequences, and subagent transcript locality.

**Evidence Inspect fails:**
- Invalid refs, missing sessions, stale refs, and missing generated artifacts return JSON errors.
- `inspect --ref session/<id>` works without `--q` and returns generated artifact metadata plus subagent refs.
- `inspect --ref session/<id> --q TEXT` requires the Clean Transcript file to exist, because snippets cannot be produced without it.
- `inspect --ref skill/<id>/<sequence>` returns primary transcript artifact metadata and locator/preview fields only; it does not inline full Clean Transcripts or subagent transcripts.
- Tool/question inspect requires the Tool Log file and sequence section.
- Subagent inspect requires the selected Subagent Run transcript.

**Transcript not generated:**
- `worker | transcript written` should appear — if missing, check for errors before it.
- Run `uv run cli.py status --fix` to identify and repair dangling paths.
- Run `uv run cli.py backfill --force --session SESSION_ID` to regenerate deterministic artifacts/fact tables for one session.

**Current session lookup fails:**
- `uv run cli.py current` works only inside an active runtime that exposes exact Session Index identity.
- Required public env: `SESSION_INDEX_SESSION_ID`, `SESSION_INDEX_NATIVE_SESSION_ID`, `SESSION_INDEX_SOURCE`, and `SESSION_INDEX_SOURCE_PATH`.
- Optional public env: `SESSION_INDEX_LEAF_ID` for Pi leaf metadata; it is reported as `leaf_id` in JSON when available.
- `source_path` is the raw provider Source Transcript, `transcript_path` is the generated Clean Transcript artifact, and `tool_log_path` is the generated Tool Log artifact.
- The Clean Transcript and Tool Log paths are derived from the Canonical Session ID under `~/.session-index/transcripts/`; a database row is not required.
- Missing or inconsistent runtime identity exits non-zero by design. v1 does not fall back to the latest session, focused terminal, registry state, or the database.

---

## Canonical troubleshooting workflow

1. Identify or narrow candidates:
   ```bash
   uv run cli.py find --topic "session index" --project session-index --limit 5
   uv run cli.py query --schema
   ```

2. Inspect generated artifact metadata without loading text:
   ```bash
   uv run cli.py inspect --ref session/pi:abc
   ```

3. Inspect scoped text only after selecting a ref:
   ```bash
   uv run cli.py inspect --ref session/pi:abc --q "the exact topic"
   uv run cli.py inspect --ref tool/pi:abc/12
   uv run cli.py inspect --ref subagent/pi:abc/0 --q "task result"
   ```

4. Read generated artifact files directly only when `inspect` is insufficient:
   ```bash
   cat ~/.session-index/transcripts/{session_id}.md
   cat ~/.session-index/transcripts/{session_id}.tools.md
   ```

5. Fall back to raw Source Transcript JSONL only for provider-native details not normalized into generated artifacts.

---

## Evaluating Cross-Project Injection

The SessionStart hook injects recent cross-project sessions as context. To measure whether this is useful, run:

```bash
uv run tests/eval_cross_project.py
uv run tests/eval_cross_project.py --verbose
uv run tests/eval_cross_project.py --since 2026-04-01
```

The script simulates what SessionStart would have injected and checks if those project names appear in the conversation transcript.

---

## Hook Implementation Details

### `session_start.py` (SessionStart)

1. Queries DB for recent same-project sessions (last 5)
2. Queries DB for recent cross-project sessions (last 10)
3. Formats results as a system-reminder block injected into the conversation

### `stop.py` (Stop)

1. Parses the session Source Transcript for deterministic fields
2. Upserts to DB — no LLM call, fast enough to run synchronously
3. Skips sessions without at least 1 user + 1 assistant message

### `session_end.py` + `_session_end_worker.py` (SessionEnd)

1. `session_end.py` forks a detached worker process and exits immediately (< 1s)
2. Worker renders the Clean Transcript in memory and generates an LLM summary via headless Pi print mode
3. Worker writes Clean Transcript and Tool Log when tool calls exist
4. Worker upserts all fields to DB and replaces fact-table rows
5. All failures are caught and logged — worker never crashes silently

### settings.json Hook Registration

| Hook Script | Event | Timeout | Async |
|-------------|-------|---------|-------|
| `session_start.py` | `SessionStart` | 5s | yes |
| `stop.py` | `Stop` | 5s | yes |
| `session_end.py` | `SessionEnd` | 1s | no |
