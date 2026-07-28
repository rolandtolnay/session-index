---
name: session-search
description: Search past Claude Code, Pi, and Codex conversations by topic, file, project, decision, tool use, skill use, subagent runs, questions, and File Mutations
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
5. Use `footprint` to audit generated artifact disk usage and prune eligibility.
6. Use `prune` only for explicit low-value session IDs after reviewing dry-run output.
7. Prefer generated Clean Transcripts, Tool Logs, and Subagent Run transcripts. Do not read raw JSONL unless generated artifacts are insufficient.

Most lookup tasks are either:

- `find` → choose a candidate by `session.summary` and `match` → `inspect --ref ...`
- `query --schema` → SQL aggregate/custom rows → construct refs → `inspect --ref ...`

## Commands

### current — identify this active session

```bash
uv run ~/.pi/agent/skills/session-search/scripts/current.py          # Canonical Session ID
uv run ~/.pi/agent/skills/session-search/scripts/current.py --path   # Clean Transcript path; warns if missing
uv run ~/.pi/agent/skills/session-search/scripts/current.py --cleaned-paths # Clean Transcript + Tool Log paths and status
uv run ~/.pi/agent/skills/session-search/scripts/current.py --native # provider-native session ID
uv run ~/.pi/agent/skills/session-search/scripts/current.py --json   # structured current-session metadata
```

Use `current` only inside an active runtime exposing exact Session Index or provider-native identity. Codex resolves `CODEX_THREAD_ID` to exactly one active or archived rollout. It does not guess from latest sessions or the database.

### query — read-only SQL over fact tables

```bash
uv run ~/.pi/agent/skills/session-search/scripts/query.py --schema
uv run ~/.pi/agent/skills/session-search/scripts/query.py "SELECT ..." [--json] [--limit N]
```

Use `query` for aggregate questions: counts by tool/project/date, recommended-answer rates, exact File Mutation lists, subagent usage, skill usage, and custom joins. It runs one read-only `SELECT`/`WITH` statement, row-capped (default 50, max 1000). SQL errors print verbatim so you can correct and retry.

Run `query --schema` for a curated LLM-oriented reference: table purposes, key columns, important semantics, Inspection Reference construction, and copyable SQL examples. It is not raw DDL.

Key tables:

- `tool_calls` — one row per tool call. Construct `tool/<session_id>/<sequence>`.
- `skill_invocations` — canonical Skill Invocation audit table for reusable prompt/workflow template use, including slash commands, Pi skill envelopes, provider Skill tools, and exact `SKILL.md` reads. Construct `skill/<session_id>/<sequence>`.
- `file_mutations` — one row per successful write/edit path. Use this for precise mutation lists and event trails.
- `subagent_runs` — one row per Subagent Run. Construct `subagent/<parent_session_id>/<child_index>` when `child_index` is present.
- `question_answers` — one row per asked question. Construct `question/<session_id>/<sequence>/<question_index>`.
- `sessions` — session metadata useful for joins: `session_id`, `project`, `branch`, `started_at`, searchable `summary`, compact `headline`, interaction counts, and generated artifact paths.

### find — compact Evidence Find candidates

```bash
uv run ~/.pi/agent/skills/session-search/scripts/find.py [criteria] [filters]
```

Criteria:

- `--topic TEXT` — session/topic candidates with `session/<session_id>` refs. Exact topic FTS is primary; if exact topic scope is empty, deterministic fuzzy fallback ranks already-indexed session metadata and still honors `--project`, `--since`, `--until`, and `--session`.
- `--tool NAME` — Tool Call candidates with `tool/<session_id>/<sequence>` refs.
- `--skill NAME` — Skill Invocation candidates with `skill/<session_id>/<sequence>` refs from `skill_invocations`.
- `--mutated PATH_FRAGMENT` — session-collapsed File Mutation candidates by default, one `session/<session_id>` ref per Canonical Session ID that mutated matching paths.
- `--mutation-mode event` — with `--mutated`, return exact event-level File Mutation rows with `tool/<session_id>/<sequence>` refs.
- `--subagent NAME` — Subagent Run candidates with `subagent/<session_id>/<child_index>` refs and parent-call refs when available.
- `--tool question --question-recommended true|false` — question-answer candidates with question refs.

Filters compose with criteria: `--project`, `--since`, `--until`, `--session`, and `--limit`. `--skill` does not compose with `--tool` because Skill Invocations are not Tool Calls.

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
- `skill/<session_id>/<sequence>` — returns Skill Invocation metadata, locator/preview fields, and primary transcript artifact metadata without inlining the full transcript. Parent invocations use the Clean Transcript as primary; subagent-scope invocations use the subagent transcript as primary and include the parent Clean Transcript as context when available.
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
uv run ~/.pi/agent/skills/session-search/scripts/inspect.py --ref skill/pi:abc/1
uv run ~/.pi/agent/skills/session-search/scripts/inspect.py --ref tool/pi:abc/12
uv run ~/.pi/agent/skills/session-search/scripts/inspect.py --ref subagent/pi:abc/0 --q "task result"
```

### footprint — generated artifact audit

```bash
uv run ~/.pi/agent/skills/session-search/scripts/footprint.py [--session ID] [--project NAME] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--limit N] [--json]
```

Use `footprint` when users ask where Session Index disk usage is going or which sessions are safe prune candidates. It reports generated Clean Transcript, Tool Log, and Subagent Run transcript sizes; missing/dangling generated paths; source JSONL retention; fact counts; and prune blockers. It never deletes anything.

### prune — confirmed low-value deletion

```bash
uv run ~/.pi/agent/skills/session-search/scripts/prune.py SESSION_ID [SESSION_ID ...]
uv run ~/.pi/agent/skills/session-search/scripts/prune.py SESSION_ID [SESSION_ID ...] --confirm
```

`prune` is dry-run by default. It deletes only exact Canonical Session IDs supplied on the command line, only when `--confirm` is present, and only when the audit classifies every requested session as low-value. Low-value means the summary has an explicit low-value signal and there are no durable facts for File Mutations, Skill Invocations, Subagent Runs, or question answers. Uncertain cases default to keep. Source JSONL is never deleted.

## Transcript storage

Generated artifacts are the normal evidence path:

- `~/.session-index/transcripts/<session-id>.md` — Clean Transcript.
- `~/.session-index/transcripts/<session-id>.tools.md` — Tool Log with ordered tool calls, arguments, status, compact read-only result excerpts, compact large write/edit argument text with hashes, and larger bounded audit excerpts for mutations/errors.
- `~/.session-index/transcripts/<session-id>/agent-*.md` — Subagent Run transcripts.

These are more compact than raw JSONL at `~/.claude/projects/`, `~/.pi/agent/sessions/`, or Codex rollout files under `~/.codex/sessions/` and `~/.codex/archived_sessions/`. Prefer them as fallback when `inspect` is insufficient.

## When to use this skill

Invoke this skill when the user references past work, asks about prior decisions, wants to audit tool/skill/subagent/question/File Mutation behavior, asks for PR summaries/changelogs from recent work, or needs counts/aggregates across sessions.

Up to seven Top-Level current-project Session Headlines and 21 ranked Top-Level cross-project headlines may already be injected with a shared Clean Transcript root and canonical transcript filenames. Nested Subagent Run sessions do not participate. Use this skill when the desired session is absent, or for specific topic lookups, structured audits, and aggregate questions.
