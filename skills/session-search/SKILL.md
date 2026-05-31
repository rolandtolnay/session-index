---
name: session-search
description: Search past Claude Code and Pi conversations by topic, file, project, decision, tool use, skill use, subagent runs, questions, and File Mutations
user_invocable: true
arguments:
  - name: query
    description: Search terms, project filter, date range, or deterministic evidence criteria
    required: false
---

# Session Search

Search and inspect past Claude Code and Pi conversations indexed in `~/.session-index`.

## Decision tree

1. Use `current` for the active conversation only.
2. Use `query` for counts, rankings, aggregates, custom grouping, and raw SQL.
3. Use `find` for compact past-session/event candidates with Inspection References.
4. Use `inspect` only on selected refs copied from `find` to retrieve bounded evidence text.
5. Prefer generated Clean Transcripts, Tool Logs, and Subagent Run transcripts over raw JSONL.

Most audit questions should be `find` first, then `inspect` one or two selected refs. Do not read raw JSONL unless the generated artifacts are insufficient.

## Commands

### current — identify this active session

```bash
uv run ~/.pi/agent/skills/session-search/scripts/current.py          # Canonical Session ID
uv run ~/.pi/agent/skills/session-search/scripts/current.py --path   # Clean Transcript path; warns if missing
uv run ~/.pi/agent/skills/session-search/scripts/current.py --native # provider-native session ID
uv run ~/.pi/agent/skills/session-search/scripts/current.py --json   # structured IDs and artifact paths
```

Claude Code path, if needed:

```bash
uv run ~/.claude/skills/session-search/scripts/current.py --json
```

Use `current --path` or `current --json` only for the exact active runtime session. It does not guess from latest sessions or the database.

### query — read-only SQL over structured fact tables

```bash
uv run ~/.pi/agent/skills/session-search/scripts/query.py --schema
uv run ~/.pi/agent/skills/session-search/scripts/query.py "SELECT ..." [--json] [--limit N]
```

Use `query` for aggregate questions: most tool calls, counts by project/date, recommended-answer rates, exact File Mutation lists, custom joins, and schema discovery. It runs one read-only `SELECT`/`WITH` statement, row-capped (default 50, max 1000). SQL errors print verbatim so you can correct and retry.

Key tables:

- `tool_calls` — one row per tool call: `session_id, source, scope, sequence, timestamp, tool_name, tool, is_error, skill_name`.
- `file_mutations` — one row per successful write/edit path. Use this for precise mutation lists and aggregates; `sessions.files_touched` is broad metadata.
- `subagent_runs` — one row per Subagent Run: `parent_session_id, requested_agent_type, observed_agent_type, call_sequence, child_index, agent_id, transcript_path, task_preview, match_confidence`, etc.
- `question_answers` — one row per asked question: `session_id, sequence, question_index, question, selected_label, was_recommended, is_other, option_count, multi_select`.

Run `query --schema` for exact DDL and examples. If you need evidence text after a SQL result, select or construct refs such as `tool/<session_id>/<sequence>` and pass them to `inspect`.

### find — compact Evidence Find candidates

```bash
uv run ~/.pi/agent/skills/session-search/scripts/find.py [criteria] [filters]
```

Criteria:

- `--topic TEXT` — session-level topic candidates with `session/<session_id>` refs.
- `--tool NAME` — Tool Call event candidates with `tool/<session_id>/<sequence>` refs.
- `--skill NAME` — skill invocation candidates with `tool/<session_id>/<sequence>` refs.
- `--mutated PATH_FRAGMENT` — File Mutation candidates from `file_mutations` with `tool/<session_id>/<sequence>` refs.
- `--subagent NAME` — Subagent Run candidates with `subagent/<session_id>/<child_index>` refs and parent-call refs when available.
- `--tool question --question-recommended true|false` — question-answer candidates with `question/<session_id>/<sequence>/<question_index>` refs.

Filters compose with the criteria:

- `--project NAME` — project prefix filter.
- `--since YYYY-MM-DD` / `--until YYYY-MM-DD` — date range.
- `--session ID` — canonical session id.
- `--limit N` — result cap.

`find` emits JSON only. It includes compact session summaries, match metadata, artifact paths, and `inspect_refs`; it never includes Clean Transcript, Tool Log, or subagent transcript evidence text.

Examples:

```bash
uv run ~/.pi/agent/skills/session-search/scripts/find.py --topic "session index" --limit 5
uv run ~/.pi/agent/skills/session-search/scripts/find.py --tool edit --project session-index
uv run ~/.pi/agent/skills/session-search/scripts/find.py --skill review
uv run ~/.pi/agent/skills/session-search/scripts/find.py --mutated "etc/prd" --since 2026-05-01
uv run ~/.pi/agent/skills/session-search/scripts/find.py --subagent scout
uv run ~/.pi/agent/skills/session-search/scripts/find.py --tool question --question-recommended false
```

### inspect — scoped Evidence Inspect packets

```bash
uv run ~/.pi/agent/skills/session-search/scripts/inspect.py --ref REF [--q TEXT] [--max-snippets N]
```

Use refs copied unchanged from `find`:

- `session/<session_id>` — requires `--q TEXT`; returns bounded Clean Transcript excerpts.
- `tool/<session_id>/<sequence>` — returns the matching Tool Log section plus associated File Mutation paths.
- `question/<session_id>/<sequence>/<question_index>` — returns question-answer metadata plus the Tool Log section.
- `subagent/<session_id>/<child_index>` — returns task/prompt-area evidence by default; with `--q`, returns query-focused subagent transcript excerpts.

`inspect` emits JSON Evidence Packets with artifact path, locator metadata, and bounded evidence text. Invalid refs, missing sessions, stale refs, and missing artifacts return JSON errors and a non-zero exit status.

Examples:

```bash
uv run ~/.pi/agent/skills/session-search/scripts/inspect.py --ref session/pi:abc --q "session index"
uv run ~/.pi/agent/skills/session-search/scripts/inspect.py --ref tool/pi:abc/12
uv run ~/.pi/agent/skills/session-search/scripts/inspect.py --ref subagent/pi:abc/0 --q "task result"
```

## Transcript storage

Generated artifacts are the normal evidence path:

- `~/.session-index/transcripts/<session-id>.md` — Clean Transcript.
- `~/.session-index/transcripts/<session-id>.tools.md` — Tool Log with ordered tool calls, arguments, status, and capped result text.
- `~/.session-index/transcripts/<session-id>/agent-*.md` — Subagent Run transcripts.

These are more compact than raw JSONL at `~/.claude/projects/` or `~/.pi/agent/sessions/`. Prefer them as fallback when `inspect` is insufficient.

## When to use this skill

Invoke this skill when the user references past work, asks about prior decisions, wants to audit tool/skill/subagent/question/File Mutation behavior, asks for PR summaries/changelogs from recent work, or needs counts/aggregates across sessions.

Recent same-project sessions may already be injected automatically. Use this skill for older sessions, other projects, specific topic lookups, structured audits, and aggregate questions.
