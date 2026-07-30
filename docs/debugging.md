# Debugging Guide

Architecture details, log format, and troubleshooting for session-index.

---

## Architecture Overview

```
Claude settings.json hooks config
    │
    ├─ SessionStart ──────► session_start.py ──► inject recent context into conversation
    ├─ Stop ──────────────► stop.py ───────────► queue turn refresh
    └─ SessionEnd ────────► session_end.py ────► queue forced final refresh

Pi extension
    │
    ├─ before_agent_start ─► pi_context.py ──► inject recent context into system prompt
    ├─ agent_end ──────────► pi_index.py --mode turn
    ├─ /current-session Ctrl+R ─► pi_index.py --mode full for the focused current snapshot
    └─ session_shutdown ───► pi_index.py --mode exit (except extension reload)

Codex hooks.json
    │
    └─ Stop ───────────────► codex_stop.py ──► queue turn refresh

Shared active-session coordinator
    │
    └─ _session_refresh_worker.py
         ├─ deterministic artifacts immediately after each queued assistant turn
         ├─ first qualifying summary/headline immediately
         ├─ later summary/headline after 180 idle seconds or 10,000 new rendered characters
         ├─ 60-second cooldown between content-trigger attempts
         └─ forced final summary for Claude SessionEnd and Pi shutdown

Codex exposes no distinct session-exit event; its latest Stop is finalized by the idle path.

Shared full pass:
    parser adapter ─► rich transcript render ─► LLM summary via headless Pi ─► Session Headline via separate headless Pi process
                                                                                 ├─► Clean Transcript + Tool Log ─► DB upsert + fact tables
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
| `hooks/stop.py` | Claude Stop: queues an active-session turn refresh |
| `hooks/session_end.py` | Claude SessionEnd: queues a forced final refresh |
| `hooks/session_refresh.py` | Provider-neutral atomic refresh queue and detached-worker launcher |
| `hooks/_session_refresh_worker.py` | Per-session deterministic/summary refresh coordinator |
| `hooks/_session_end_worker.py` | Compatibility entry point for older Claude installs |
| `hooks/pi_index.py` | Pi extension entry point for automatic turn/exit and manual full indexing |
| `hooks/pi_context.py` | Pi extension entry point for recent-context system prompt injection |
| `hooks/codex_stop.py` | Codex Stop: queue the latest rollout snapshot and exit immediately |
| `hooks/_codex_index_worker.py` | Compatibility entry point delegating to the shared coordinator |
| `pi-extension/index.ts` | Pi extension wiring for lifecycle events |
| `pi-extension/session-index-env.ts` | Pi runtime environment helper for current-session lookup |
| `current_session.py` | Exact current-session resolver using Session Index runtime env |
| `indexer.py` | Shared parse/summarize/transcript/upsert pipeline |
| `sources.py` | Claude/Pi/Codex Source Transcript discovery for backfill and hook fallback |
| `recent_context.py` | Shared recent-session context builder |
| `cli.py` | CLI entry point: current, find, inspect, query, backfill, status |
| `db.py` | SQLite operations: provider-aware schema, FTS-backed candidate lookup, read-only query helpers, stats |
| `evidence_find.py` | Evidence Find candidate retrieval and JSON construction |
| `evidence_inspect.py` | Evidence Inspect reference resolution and packet construction |
| `inspect_refs.py` | Inspection Reference parsing/formatting |
| `transcript.py` | Clean Transcript writer + Evidence Snippet selector |
| `tool_log.py` | Per-session Markdown Tool Log writer and section extractor |
| `skill_facts.py` | Canonical Skill Invocation extraction and row building |
| `summarizer.py` | LLM summary generator plus separate Session Headline generator using headless Pi, with legacy summary fallback |
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
| Codex Source Transcript | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` | Codex managed |
| Active refresh jobs/state | `~/.session-index/refresh-jobs/{source}/{session-id}/` | Pending jobs are consumed after covered summary attempts; summary watermarks persist |

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
- `hook_name` — left-padded to 18 chars. Common values: `session_start`, `session_end`, `stop`, `pi_index`, `pi_context`, `codex_stop`, `refresh_worker`, `query`
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
15:32:45.200 [a1b2c3] stop               | queued 001-stop.json
15:32:45.230 [a1b2c3] refresh_worker     | deterministic indexed (2 msgs)
15:32:48.500 [a1b2c3] refresh_worker     | summary generated (initial)
15:35:10.400 [a1b2c3] stop               | queued 002-stop.json
15:35:10.430 [a1b2c3] refresh_worker     | deterministic indexed (4 msgs)
15:38:10.500 [a1b2c3] refresh_worker     | summary generated (idle)
15:40:00.100 [a1b2c3] session_end        | queued final refresh 003-session-end.json
15:40:00.130 [a1b2c3] refresh_worker     | deterministic indexed (4 msgs)
15:40:03.500 [a1b2c3] refresh_worker     | summary generated (force)
```

---

## Diagnosing Common Issues

**Session not indexed:**
- `refresh_worker | deterministic skipped (N user, M assistant msgs)` — needs at least 1 user + 1 assistant message
- Claude: no `stop | queued` line — the Stop hook did not fire
- Pi: no `pi_index | turn queued` line — run `/reload` or restart Pi after installing the extension
- Codex: no `codex_stop` lines — restart Codex, open `/hooks`, and review/trust the Session Index hook
- `refresh_worker | source transcript missing` / `pi_index | missing session file` — Source Transcript path mismatch

**Summary or Session Headline missing:**
- Check Pi auth/model availability: default is `openai-codex/gpt-5.4-mini` via `pi -p --no-session --no-tools`.
- Headlines use a second isolated Pi process after successful summary generation; a failed headline call preserves any previous value.
- Set `SESSION_INDEX_SUMMARY_MODEL`, `SESSION_INDEX_SUMMARY_THINKING`, or `SESSION_INDEX_SUMMARY_TIMEOUT` to override the default.
- Automatic refresh defaults are 180 idle seconds, 10,000 new rendered characters, and a 60-second content-trigger cooldown. Override them with `SESSION_INDEX_SUMMARY_IDLE_SECONDS`, `SESSION_INDEX_SUMMARY_CONTENT_CHARS`, and `SESSION_INDEX_SUMMARY_CONTENT_COOLDOWN_SECONDS`.
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
- `refresh_worker | deterministic indexed` should appear after the provider queues a turn — if missing, inspect preceding refresh-worker errors.
- Run `uv run cli.py status --fix` to identify and repair dangling paths.
- Run `uv run cli.py backfill --force --session SESSION_ID` to regenerate deterministic artifacts/fact tables for one session.

**Current session lookup fails:**
- `uv run cli.py current` works only inside an active runtime that exposes exact Session Index identity.
- Required public env: `SESSION_INDEX_SESSION_ID`, `SESSION_INDEX_NATIVE_SESSION_ID`, `SESSION_INDEX_SOURCE`, and `SESSION_INDEX_SOURCE_PATH`.
- Optional public env: `SESSION_INDEX_LEAF_ID` for Pi leaf metadata; it is reported as `leaf_id` in JSON when available.
- Codex compatibility uses `CODEX_THREAD_ID` and requires exactly one matching rollout under the active or archived Codex session directories.
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

1. Selects the latest seven Top-Level current-project sessions with headlines and existing Clean Transcripts; nested Pi subagent `run-N/session.jsonl` rows are excluded before limiting.
2. Selects Top-Level other-project candidates from the last seven days and ranks them by 60% total-turn percentile plus 40% assistant-character percentile, with recency as the tie-breaker.
3. Injects one shared Clean Transcript root, retaining branch names for current-project entries while cross-project entries contain only dates, projects, canonical transcript filenames, and headlines.
4. Directs the agent to `session-search` when the needed session is absent.

Pi's `before_agent_start` path calls the same `recent_context.py` builder, so selection and formatting are identical across Claude and Pi.

### Shared active-session refresh

1. Claude Stop, Pi `agent_end`, and Codex Stop atomically queue the latest Source Transcript snapshot and ensure one detached coordinator exists for the Canonical Session ID.
2. The coordinator coalesces pending events and immediately runs the complete deterministic pass: metadata, Clean Transcript, Tool Log, Subagent Run transcripts, and structured fact tables.
3. The first qualifying snapshot (at least one user and one assistant message) immediately attempts a Session Summary and Session Headline.
4. Later descriptions refresh after 180 seconds without a newer assistant turn or after 10,000 newly rendered user/assistant characters since the last successful summary. Content-trigger attempts have a 60-second cooldown.
5. Failed summary/headline generation preserves prior descriptions and never advances the successful-summary content watermark.
6. Claude SessionEnd and Pi non-reload shutdown queue a forced final refresh. Codex has no distinct session-exit event, so its latest Stop uses the idle path.
7. Workers and hook boundaries catch/log failures; provider hooks never wait for deterministic indexing or LLM calls.

Nested Pi subagent lifecycle sessions are skipped by the shared indexer. Their activity remains represented through the parent session's Subagent Run facts and generated subagent transcript rather than a duplicate top-level session row.

### settings.json Hook Registration

| Hook Script | Event | Timeout | Async |
|-------------|-------|---------|-------|
| `session_start.py` | `SessionStart` | 5s | yes |
| `stop.py` | `Stop` | 10s | detached coordinator |
| `session_end.py` | `SessionEnd` | 5s | detached coordinator |

Codex registers `codex_stop.py` as a 5-second command handler under `Stop` in `~/.codex/hooks.json`. Codex must review and trust the exact hook definition before it runs.
