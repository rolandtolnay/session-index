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
    └─ session_shutdown ───► pi_index.py --mode full

Shared full pass:
    parser adapter ─► LLM summary via Ollama ─► cleaned transcript ─► DB upsert

Search path (skill invocation):
    search.py (skill wrapper) ──► cmd_search() in cli.py ──► FTS5 query ──► formatted output
                                                          └─► log entry to session-index.log
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
| `indexer.py` | Shared parse/summarize/transcript/upsert pipeline |
| `sources.py` | Claude/Pi source JSONL discovery for backfill |
| `recent_context.py` | Shared recent-session context builder |
| `cli.py` | CLI entry point: search, excerpt, backfill, status commands |
| `db.py` | SQLite operations: provider-aware schema, upsert, FTS5 search, stats |
| `parser.py` | Claude JSONL parser |
| `pi_parser.py` | Pi tree-structured JSONL parser |
| `transcript.py` | Transcript writer + excerpt extractor for search results |
| `summarizer.py` | LLM summary generator using local Ollama model |
| `logger.py` | Structured logging with monthly rotation |
| `client.py` | Standalone Ollama HTTP client (pure stdlib) |
| `skills/session-search/scripts/search.py` | Skill wrapper: argparse → `cmd_search()` |
| `skills/session-search/scripts/excerpt.py` | Skill wrapper: argparse → `cmd_excerpt()` |
| `skills/session-search/SKILL.md` | Skill instructions for Claude Code and Pi agents |

---

## Data Locations

| Data | Path | Lifetime |
|------|------|----------|
| Database | `~/.session-index/sessions.db` | Permanent |
| Transcripts | `~/.session-index/transcripts/{session_id}.md` | Permanent |
| Log (current month) | `~/.session-index/logs/session-index.log` | Monthly rotation |
| Log (previous month) | `~/.session-index/logs/session-index.prev.log` | Overwritten monthly |
| Claude source JSONL | `~/.claude/projects/{encoded_path}/{session_id}.jsonl` | Claude Code managed |
| Pi source JSONL | `~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl` | Pi managed |

---

## Debugging

### Log File

Location: `~/.session-index/logs/session-index.log`. Rotates monthly — current + previous month retained.

All hooks and search calls log every code path. If a hook fires and there is **no log line at all**, it means the hook runner itself failed before the script executed.

### Log Format

```
HH:MM:SS.mmm [sid] hook_name          | message
```

- `HH:MM:SS.mmm` — wall-clock timestamp with millisecond precision
- `[sid]` — last 6 characters of the session ID (or `??????` if unavailable)
- `hook_name` — left-padded to 18 chars. Common values: `session_start`, `session_end`, `worker`, `stop`, `pi_index`, `pi_context`, `search`
- `message` — free-form, action-oriented

### Filtering by Session

```bash
grep '\[abc123\]' ~/.session-index/logs/session-index.log
```

The `[sid]` tag links all activity for a session: hook events, worker progress, and search calls made during that conversation.

### Example: Full Session Lifecycle

```
15:30:01.100 [a1b2c3] session_start      | started
15:30:01.130 [a1b2c3] session_start      | injected 3 same + 8 cross
15:32:45.200 [a1b2c3] stop               | started
15:32:45.230 [a1b2c3] stop               | upserted (4 msgs, 2 files)
15:32:46.100 [a1b2c3] search             | query="auth middleware" project=dashboard-web excerpt=true -> 3 results (12ms)
15:32:47.500 [a1b2c3] search             | query="auth middleware rewrite" -> 1 results (8ms)
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
- Claude: no `session_end` or `worker` lines — session still active, or SessionEnd hook didn't fire
- Pi: no `pi_index` lines — run `/reload` or restart Pi after installing the extension
- `worker | jsonl not found` / `pi_index | missing session file` — source JSONL path mismatch

**Summary missing:**
- `worker | llm error: ...` — Ollama not running or model unavailable
- `worker | llm summary empty` — model returned empty response
- Run `uv run cli.py status` to find sessions missing summaries

**Search returns no results:**
- Check the log for `search | query="..." -> 0 results` to confirm the search ran
- FTS5 tokenization: queries like `COOLDOWN_SECONDS` won't match because FTS5 splits on underscores. Try `cooldown seconds` instead
- Project filter is prefix match: `--project ghostty` matches `ghostty-peon` but `--project ghostty-peon` won't match sessions indexed under `.claude` (the project before repo extraction)
- Date filters are inclusive: `--since 2026-03-17 --until 2026-03-18` includes both days

**Transcript not generated:**
- `worker | transcript written` should appear — if missing, check for errors before it
- Run `uv run cli.py status --fix` to identify and repair dangling paths

---

## Evaluating Cross-Project Injection

The SessionStart hook injects recent cross-project sessions as context. To measure whether this is useful, run the evaluation script:

```bash
uv run tests/eval_cross_project.py
uv run tests/eval_cross_project.py --verbose          # show per-session hits
uv run tests/eval_cross_project.py --since 2026-04-01  # only recent sessions
```

The script simulates what the SessionStart hook would have injected for each session (cross-project sessions in the prior 24h), then checks if those project names appear in the conversation transcript. It reports a hit rate — how often cross-project context was actually referenced.

**Interpreting results:**
- **< 10%** — injection is mostly noise, consider removing or making it opt-in
- **10-30%** — useful enough to keep, but consider a lighter format (project names only, no summaries)
- **> 30%** — high value, keep as-is

Note: the system was installed on **2026-04-02**. Sessions before that date are backfilled and lack injection context, so use `--since 2026-04-01` for meaningful results. Without the filter, the script measures cross-project *relevance* (whether the topic came up) rather than *usefulness* (whether the injection influenced the conversation).

---

## Auditing Search Effectiveness

The search log enables mechanical auditing: hit rates, common failure patterns, and filter usage. For intent-based auditing (was this the right search for what the user wanted?), cross-reference with the session's raw JSONL.

### Three-source audit workflow

Each search call exists in up to three places with different detail levels:

| Source | Contains | Limitation |
|--------|----------|------------|
| **Log** (`session-index.log`) | Query, flags, result count, duration | No results content, no user intent |
| **Cleaned transcript** (`transcripts/{sid}.md`) | Conversation narrative around the search | Exact commands and outputs stripped |
| **Raw JSONL** (`~/.claude/projects/.../{sid}.jsonl`) | Full Bash commands, outputs, surrounding messages | Ephemeral (~3 months), requires JSON parsing |

### Step-by-step audit

1. **Pull search calls from the log:**
   ```bash
   grep '\] search' ~/.session-index/logs/session-index.log
   ```

2. **Identify patterns** — look for:
   - `-> 0 results` entries (failed searches)
   - Multiple searches from the same `[sid]` (progressive narrowing/broadening)
   - Repeated queries across sessions (systemic gaps)

3. **For flagged sessions, check the JSONL** (if it still exists):
   ```bash
   # Find the full session ID from the sid suffix
   ls ~/.claude/projects/*/ | grep 'abc123'
   
   # Extract search commands and their outputs
   python3 -c "
   import json
   with open('path/to/session.jsonl') as f:
       for line in f:
           entry = json.loads(line.strip())
           msg = entry.get('message', {})
           if not isinstance(msg, dict): continue
           content = msg.get('content', '')
           if isinstance(content, list):
               for block in content:
                   if isinstance(block, dict) and block.get('type') == 'tool_use':
                       cmd = block.get('input', {}).get('command', '')
                       if 'search.py' in cmd:
                           print(f'CALL: {cmd}')
                   if isinstance(block, dict) and block.get('type') == 'tool_result':
                       c = block.get('content', '')
                       if isinstance(c, str) and ('result' in c or 'No results' in c):
                           print(f'  -> {c[:120]}')
   "
   ```

4. **Read the transcript for context** — understand what the user was trying to find:
   ```bash
   # The transcript has the conversation flow but not exact commands
   cat ~/.session-index/transcripts/{session_id}.md
   ```

### Known audit limitations

- **Log ↔ transcript reconciliation**: Both the log and the cleaned transcript include timestamps (`HH:MM:SS` format). Match a search log entry's timestamp to the nearest `[user]` or `[assistant]` timestamp in the transcript to locate the surrounding conversation context. Transcripts generated before per-message timestamps were added (pre-2026-04) lack these markers — re-run `backfill --transcripts-only --force` to regenerate them.
- **JSONL expiry**: Full audit (with command-level detail) is only possible while the JSONL still exists (~3 months). After that, only the log + transcript remain.
- **Cross-session queries**: The `[sid]` in the log is the session that *ran* the search, not the sessions that were *found*. To audit result quality, you need the JSONL.

---

## Hook Implementation Details

### `session_start.py` (SessionStart)

1. Queries DB for recent same-project sessions (last 5)
2. Queries DB for recent cross-project sessions (last 10)
3. Formats results as a system-reminder block injected into the conversation

### `stop.py` (Stop)

1. Parses the session's JSONL for deterministic fields (message counts, files touched)
2. Upserts to DB — no LLM call, fast enough to run synchronously
3. Skips sessions without at least 1 user + 1 assistant message

### `session_end.py` + `_session_end_worker.py` (SessionEnd)

1. `session_end.py` forks a detached worker process and exits immediately (< 1s)
2. Worker generates LLM summary via local Ollama (bounded by 8192-token context)
3. Worker writes cleaned Markdown transcript
4. Worker upserts all fields to DB (summary, transcript_path, slug)
5. All failures are caught and logged — worker never crashes silently

### settings.json Hook Registration

| Hook Script | Event | Timeout | Async |
|-------------|-------|---------|-------|
| `session_start.py` | `SessionStart` | 5s | yes |
| `stop.py` | `Stop` | 5s | yes |
| `session_end.py` | `SessionEnd` | 1s | no |
