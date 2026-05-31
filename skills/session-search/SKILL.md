---
name: session-search
description: Search past Claude Code and Pi conversations by topic, file, project, or decision
user_invocable: true
arguments:
  - name: query
    description: Search terms, project filter, date range, or combination
    required: false
---

# Session Search

Search and extract content from past Claude Code and Pi conversations indexed in `~/.session-index`.

## Commands

### search — Find sessions

```bash
uv run ~/.pi/agent/skills/session-search/scripts/search.py [query] [--project NAME] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--no-any] [--limit N]
```

If you are running from Claude Code and only have the Claude skill installed, the same script is available at:

```bash
uv run ~/.claude/skills/session-search/scripts/search.py [query]
```

Returns session summaries: session_id, project, date, branch, summary, files touched, and a `tool log:` path when detailed tool-call artifacts exist.

- **query** -- FTS keywords (optional if filters given). Default: OR matching (any term matches)
- **--project** -- prefix match (e.g., `--project session` matches session-index)
- **--since / --until** -- date range filter (ISO dates)
- **--no-any** -- require ALL terms to match (AND). Default is OR
- **--limit** -- max results (default 20)
- Flags combine freely: `search.py "auth token" --project dashboard --since 2026-03-01`

### excerpt — Extract transcript passages

```bash
uv run ~/.pi/agent/skills/session-search/scripts/excerpt.py <session> [<session> ...] -q "keywords"
```

Claude Code path, if needed:

```bash
uv run ~/.claude/skills/session-search/scripts/excerpt.py <session> -q "keywords"
```

Returns focused transcript blocks from specific sessions (max 3 per call). When available, it also prints `Tool log available:` for detailed tool-call debugging.

- **session** -- session ID (or 8+ char prefix). Pi rows are stored as `pi:<uuid>` but raw UUID prefixes also resolve.
- **-q / --query** -- keywords to focus extraction (required)
- Example: `excerpt.py 07983a7f -q "auth token refresh"`
- Example: `excerpt.py 019dde8f -q "pi transcript parser"`

### query — Read-only SQL over structured fact tables

```bash
uv run ~/.pi/agent/skills/session-search/scripts/query.py "SELECT ..." [--json] [--limit N]
uv run ~/.pi/agent/skills/session-search/scripts/query.py --schema
```

Claude Code path, if needed:

```bash
uv run ~/.claude/skills/session-search/scripts/query.py "SELECT ..."
```

The escape hatch for "find/aggregate X" questions that FTS can't answer: *most X tool
calls*, *how often I picked the recommended answer*, *sessions that used skill/subagent X*,
or *which files were successfully written or edited by a session*.
Runs a single read-only `SELECT`/`WITH` statement (no writes, no multi-statement) against
the structured fact tables, row-capped (default 50, max 1000). SQL errors print verbatim so
you can correct and retry. **Run `--schema` first** to see exact columns + example queries.

- **sql** — one `SELECT` or `WITH` statement (omit when using `--schema`)
- **--schema** — print fact-table DDL, `sessions` columns, and the example queries; exit
- **--json** — rows as a JSON array (default is an aligned text table)
- **--limit** — max rows (default 50, cap 1000)

**Tables** (all keyed by session id):

- `tool_calls` — one row per tool call. Columns: `session_id, source, scope`
  (`main` or `agent-<id>`), `sequence, timestamp, tool_name` (raw), `tool`
  (lexically normalized: namespace-stripped + lowercased, e.g. `Agent` -> `agent`),
  `is_error, skill_name` (set only for `skill`/`Skill` calls). Semantic domains are
  queryable through dedicated tables (`question_answers`, `subagent_runs`, `file_mutations`).
- `file_mutations` — one row per successful File Mutation (a write/edit target path),
  excluding failed mutations, reads, searches, lists, and bash. Use this for precise
  write/edit file lists; `sessions.files_touched` remains broad search metadata. Columns:
  `session_id, source, scope, sequence, timestamp, tool_name, tool, path`.
- `subagent_runs` — one row per subagent run. Columns: `parent_session_id, source,
  requested_agent_type` (the canonical query label), `observed_agent_type, call_tool,
  call_sequence, call_tool_id, child_index, agent_id, status, started_at, ended_at,
  duration_seconds, tool_call_count, transcript_path, task_preview, match_confidence`.
- `question_answers` — one row per asked question. Columns: `session_id, source, sequence,
  question_index, header, question, selected_label, was_recommended` (1/0/NULL),
  `is_other, option_count, multi_select`.

Join to `sessions` on `tool_calls.session_id = sessions.session_id`,
`file_mutations.session_id = sessions.session_id` (or `subagent_runs.parent_session_id`)
for project/date/summary context.

```sql
-- 1. Sessions with the most direct subagent-request tool calls
SELECT session_id, COUNT(*) n FROM tool_calls
WHERE tool IN ('agent', 'subagent', 'subagent_run') AND scope='main'
GROUP BY session_id ORDER BY n DESC LIMIT 10;

-- 2. How often I picked the recommended answer (Claude + recovered Pi)
SELECT was_recommended, COUNT(*) FROM question_answers
WHERE was_recommended IS NOT NULL AND multi_select=0 GROUP BY was_recommended;

-- 3. Sessions that used a given skill
SELECT DISTINCT t.session_id, s.project, s.started_at
FROM tool_calls t JOIN sessions s ON s.session_id=t.session_id
WHERE t.skill_name='update-config' ORDER BY s.started_at DESC;

-- 4. Sessions that used a given subagent type
SELECT parent_session_id, COUNT(*) runs FROM subagent_runs
WHERE requested_agent_type='Explore' GROUP BY parent_session_id ORDER BY runs DESC;

-- 5. Files successfully written or edited in one session
SELECT DISTINCT path FROM file_mutations
WHERE session_id='SESSION_ID' ORDER BY path;

-- 6. File Mutation event trail for one session
SELECT scope, sequence, tool_name, path FROM file_mutations
WHERE session_id='SESSION_ID' ORDER BY sequence, path;
```

**Limitations:**

- Fact tables cover sessions indexed with the tool-log stage; older rows are populated by a
  one-time `uv run cli.py backfill --no-summary --force` (in the repo). If a query returns
  surprisingly few rows, the corpus may not be fully backfilled yet. Historical
  `file_mutations` coverage requires source-transcript backfill and cannot be reconstructed
  from deleted raw logs by this feature.
- `was_recommended` is NULL when the question had no `(Recommended)` option, was unanswered
  (cancelled), or is multi-select. Filter `WHERE was_recommended IS NOT NULL AND multi_select=0`
  for "picked the recommended" aggregations.
- Multi-select answers store joined labels in `selected_label` with `was_recommended=NULL`.
- Read-only: `SELECT`/`WITH` only, single statement, row-capped.

### current — Identify this active session

```bash
uv run ~/.pi/agent/skills/session-search/scripts/current.py          # Canonical Session ID
uv run ~/.pi/agent/skills/session-search/scripts/current.py --path   # cleaned transcript path; warns if missing
uv run ~/.pi/agent/skills/session-search/scripts/current.py --native # provider-native session ID
uv run ~/.pi/agent/skills/session-search/scripts/current.py --json   # structured IDs, source path, artifact paths, existence flags
```

Claude Code path, if needed:

```bash
uv run ~/.claude/skills/session-search/scripts/current.py --path
```

Use `current --path` when you need the deterministic cleaned transcript path for the conversation you are currently in. It prints the path on stdout and warns on stderr if the cleaned transcript file has not been written yet; use `current --json` for machine-readable existence flags. It works from exact runtime identity exposed via Session Index env and does not guess from latest sessions, terminals, or the database. If the active runtime does not expose that identity, it exits non-zero instead of returning a potentially wrong session.

## Workflow

1. **For this active conversation, use `current --path`.** This gives the exact cleaned transcript path without searching, with a stderr warning if the file does not exist yet.
2. **For past conversations, search first.** Run `search` to find relevant sessions by topic.
3. **Extract if needed.** Copy session ID(s) from search results, pass to `excerpt` with keywords.
4. **Fall back to reading the cleaned transcript directly** if `excerpt` returns off-topic blocks after one query refinement, or when the footer reports more agent-transcript matches you want to see.
5. **For counting/aggregation questions** (most X tool calls, recommended-answer rate, which sessions used skill/subagent X), use `query` instead of FTS — run `query --schema` first to see the columns.

Most questions are answered by summaries alone. Use `excerpt` only when you need the actual conversation content -- specific decisions, code explanations, or implementation details.

## Transcript storage

`excerpt` auto-scans subagent transcripts and reports additional matches in a footer. When you need to read more than the top hit, go to the files directly:

- `~/.session-index/transcripts/<session-id>.md` -- main session transcript (user + assistant turns)
- `~/.session-index/transcripts/<session-id>.tools.md` -- ordered tool calls, arguments, status, and capped result text; read this when debugging past tool behavior
- `~/.session-index/transcripts/<session-id>/agent-*.md` -- one file per spawned subagent, when available

These are cleaned/generated markdown, much more compact than raw JSONL at `~/.claude/projects/` or `~/.pi/agent/sessions/`. Prefer them as the fallback — do NOT read raw JSONL unless the cleaned transcript and tool log are insufficient.

## Query Tips

- Use 1-3 specific terms, not full sentences
- For decisions, use the *topic* not the *action*: `"transcript format"` not `"implemented transcript writer"`
- Browse with `--project` (no query) to orient, then query to narrow

## When to Use

Invoke this skill when the user:
- References past work in another project
- Asks about a prior decision or discussion
- Wants to find or resume a previous conversation
- Asks to generate PR summaries or changelogs from recent work
- Asks to count or aggregate across sessions (tool usage, recommended-answer rate, which sessions used a given skill or subagent) — use `query`

Note: Recent same-project sessions are already injected automatically when the relevant Claude hook or Pi extension is installed. Use this skill for older sessions, other projects, or specific topic lookups.
