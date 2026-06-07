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

Use Session Index to move from a user’s vague reference to past work into scoped, inspectable evidence. The canonical LLM-facing surface is this skill plus CLI `--help`; the README is not required for operation.

## Decision tree

1. Use `current` only for the exact active runtime conversation.
2. Use `query` for counts, rankings, aggregates, custom joins, and audits over structured fact tables.
3. Use `find` for compact Evidence Find candidates when you need likely sessions/events and Inspection References.
4. Use `inspect` on selected refs copied unchanged from `find` or constructed from SQL rows.
5. Prefer generated Clean Transcripts, Tool Logs, and Subagent Run transcripts. Do not read raw JSONL unless generated artifacts are insufficient.

Most lookup tasks are either:

- `find` → choose a candidate by `session.summary` and `match` → `inspect --ref ...`
- `query --schema` → SQL aggregate/custom rows → construct refs → `inspect --ref ...`

## Commands

### current — identify this active session

```bash
uv run ~/.pi/agent/skills/session-search/scripts/current.py          # Canonical Session ID
uv run ~/.pi/agent/skills/session-search/scripts/current.py --path   # Clean Transcript path; warns if missing
uv run ~/.pi/agent/skills/session-search/scripts/current.py --native # provider-native session ID
uv run ~/.pi/agent/skills/session-search/scripts/current.py --json   # structured current-session metadata
```

Use `current` only inside an active runtime exposing Session Index environment. It does not guess from latest sessions or the database.

### query — read-only SQL over fact tables

```bash
uv run ~/.pi/agent/skills/session-search/scripts/query.py --schema
uv run ~/.pi/agent/skills/session-search/scripts/query.py "SELECT ..." [--json] [--limit N]
```

Use `query` for aggregate questions: counts by tool/project/date, recommended-answer rates, exact File Mutation lists, subagent usage, skill usage, and custom joins. It runs one read-only `SELECT`/`WITH` statement, row-capped (default 50, max 1000). SQL errors print verbatim so you can correct and retry.

Run `query --schema` for a curated LLM-oriented reference: table purposes, key columns, important semantics, Inspection Reference construction, and copyable SQL examples. It is not raw DDL.

Key tables:

- `tool_calls` — one row per tool call. Construct `tool/<session_id>/<sequence>`.
- `file_mutations` — one row per successful write/edit path. Use this for precise mutation lists and event trails.
- `subagent_runs` — one row per Subagent Run. Construct `subagent/<parent_session_id>/<child_index>` when `child_index` is present.
- `question_answers` — one row per asked question. Construct `question/<session_id>/<sequence>/<question_index>`.
- `sessions` — session metadata useful for joins: `session_id`, `project`, `branch`, `started_at`, `summary`, generated artifact paths.

### find — compact Evidence Find candidates

```bash
uv run ~/.pi/agent/skills/session-search/scripts/find.py [criteria] [filters]
```

Criteria:

- `--topic TEXT` — session/topic candidates with `session/<session_id>` refs. Exact topic FTS is primary; if exact topic scope is empty, deterministic fuzzy fallback ranks already-indexed session metadata and still honors `--project`, `--since`, `--until`, and `--session`.
- `--tool NAME` — Tool Call candidates with `tool/<session_id>/<sequence>` refs.
- `--skill NAME` — skill invocation candidates with tool refs.
- `--mutated PATH_FRAGMENT` — session-collapsed File Mutation candidates by default, one `session/<session_id>` ref per Canonical Session ID that mutated matching paths.
- `--mutation-mode event` — with `--mutated`, return exact event-level File Mutation rows with `tool/<session_id>/<sequence>` refs.
- `--subagent NAME` — Subagent Run candidates with `subagent/<session_id>/<child_index>` refs and parent-call refs when available.
- `--tool question --question-recommended true|false` — question-answer candidates with question refs.

Filters compose with criteria: `--project`, `--since`, `--until`, `--session`, and `--limit`.

`find` emits compact JSON only. Each candidate includes `ref`, `inspect_refs`, `session`, and `match`. `session.summary` is retained because it is high-signal candidate-selection metadata. `find` does not return Evidence Snippets or broad top-level artifact inventories such as repeated Clean Transcript paths, Tool Log paths, or subagent transcript lists.

For default `find --mutated ...` results, `match.kind` is `file_mutation_session`; `match.match_count`, `match.distinct_path_count`, and `match.representative_paths` summarize only matching File Mutation rows. `inspect_refs.related_tools` contains up to five exact `tool/<session>/<sequence>` refs for drill-down without making the default result event-level again.

When topic fallback scopes a non-topic criterion, the result keeps its primary `match.kind` and includes `match.topic_scope` with `match_mode: "fuzzy_fallback"` and a score.

Candidate-specific artifact handles may appear when they shorten the path to scoped context. In particular, `find --subagent ...` keeps `match.transcript_path` for the exact matched Subagent Run.

Examples:

```bash
uv run ~/.pi/agent/skills/session-search/scripts/find.py --topic "session index" --limit 5
uv run ~/.pi/agent/skills/session-search/scripts/find.py --tool edit --project session-index
uv run ~/.pi/agent/skills/session-search/scripts/find.py --skill review
uv run ~/.pi/agent/skills/session-search/scripts/find.py --mutated "etc/prd" --since 2026-05-01
uv run ~/.pi/agent/skills/session-search/scripts/find.py --mutated "etc/prd" --mutation-mode event
uv run ~/.pi/agent/skills/session-search/scripts/find.py --subagent scout
uv run ~/.pi/agent/skills/session-search/scripts/find.py --tool question --question-recommended false
```

### inspect — scoped Evidence Inspect packets

```bash
uv run ~/.pi/agent/skills/session-search/scripts/inspect.py --ref REF [--q TEXT] [--max-snippets N]
```

Use refs copied unchanged from `find` or constructed from `query --schema` guidance:

- `session/<session_id>` — without `--q`, returns session metadata, generated artifact metadata (including the Clean Transcript artifact path/existence), structured subagent refs, and `evidence: []`; with `--q`, adds query-focused Clean Transcript Evidence Snippets.
- `tool/<session_id>/<sequence>` — returns the matching Tool Log section plus associated File Mutation paths.
- `question/<session_id>/<sequence>/<question_index>` — returns question-answer metadata plus the Tool Log section.
- `subagent/<session_id>/<child_index>` — returns task/prompt-area evidence by default; with `--q`, returns query-focused Subagent Run Evidence Snippets.

Session inspect artifact metadata has deterministic paths and existence booleans for generated artifacts:

- `artifacts.clean_transcript: {path, exists}`
- `artifacts.tool_log: {path, exists}`
- `artifacts.subagent_transcripts: {count}`

Session inspect does not expose raw Source Transcript paths and does not list every subagent transcript path. It exposes `inspect_refs.subagents[]` objects with `ref`, `requested_agent_type`, and `task_preview` so you can choose a child run before loading it.

`inspect` emits JSON Evidence Packets with artifact path, locator metadata, and bounded Evidence Snippets. Invalid refs, missing sessions, stale refs, and missing artifacts return JSON errors and a non-zero exit status.

Examples:

```bash
uv run ~/.pi/agent/skills/session-search/scripts/inspect.py --ref session/pi:abc
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
