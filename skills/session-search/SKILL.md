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

## Choosing a path

Most lookup tasks are either:

- `find` → choose a candidate by `session.summary` and `match` → `inspect --ref ...`
- `query --schema` → SQL aggregate/custom rows → construct refs → `inspect --ref ...`

Evidence escalates in tiers: scoped `inspect` packets first; whole generated artifacts (Clean Transcript, Tool Log, Subagent Run transcripts) when a packet is insufficient; raw Source JSONL only when generated artifacts are insufficient.

Stop once you have enough evidence to answer the user's question — usually one or two `inspect` calls on the best candidates. Widen the search only when the evidence in hand is insufficient or contradictory.

## Commands

Examples use the Pi install path; substitute your install's skill root (for example `~/.claude/skills/session-search`) or run `uv run cli.py <command>` from the repo. Wrappers resolve the repo through their own symlink.

### current — identify this active session

```bash
uv run ~/.pi/agent/skills/session-search/scripts/current.py          # Canonical Session ID
uv run ~/.pi/agent/skills/session-search/scripts/current.py --path   # Clean Transcript path; warns if missing
uv run ~/.pi/agent/skills/session-search/scripts/current.py --cleaned-paths # Clean Transcript + Tool Log paths and status
uv run ~/.pi/agent/skills/session-search/scripts/current.py --native # provider-native session ID
uv run ~/.pi/agent/skills/session-search/scripts/current.py --json   # structured current-session metadata
```

Use `current` only inside an active runtime exposing exact Session Index or provider-native identity. Codex resolves `CODEX_THREAD_ID` to exactly one active or archived rollout. It does not guess from latest sessions or the database.

### snapshot — fresh Clean Transcript for commit intent

```bash
uv run ~/.pi/agent/skills/session-search/scripts/snapshot.py --cwd /absolute/git/worktree --resolve-only # resolve origin before guarding its source path
uv run ~/.pi/agent/skills/session-search/scripts/snapshot.py --cwd /absolute/git/worktree --max-chars 80000
uv run ~/.pi/agent/skills/session-search/scripts/snapshot.py --cwd /absolute/git/worktree --session pi:<16-hex> --leaf-id <exact-leaf>
uv run ~/.pi/agent/skills/session-search/scripts/snapshot.py --cwd /absolute/git/worktree --source pi --source-path /exact/session.jsonl --native-session-id <getSessionId> --leaf-id <getLeafId>
```

Use `snapshot` when commit intent needs the originating conversation **now**, rather than an asynchronously generated artifact. It synchronously captures the Source Transcript once, uses existing conversation cleaning, and emits one JSON object without files, indexing, database mutation, LLM calls, Tool Logs, child transcripts or Side Chats. Existing question answers are retained. It never reads a cached Clean Transcript or guesses the latest session.

For filesystem Security Guard integration, first use `--resolve-only`. It returns only `{session_id, native_session_id, source, source_path, leaf_id}` without opening/parsing the source or reading provider metadata. It applies the same identity, argument, positive `--max-chars`, and required Pi leaf validation as capture, but does not validate source existence, content, ancestry or worktree ownership yet. Exact indexed origin lookup remains read-only; Claude and Codex compatibility discovery uses filename globs only. Guard the returned path (and its canonical filesystem path) before invoking full capture with the returned explicit source identity and leaf. Full capture rejects final-file symlinks; the caller is responsible for guarding raw/resolved parent paths.

Identity modes: exact runtime `SESSION_INDEX_*` (or Claude compatibility env / Codex `CODEX_THREAD_ID`); an exact indexed canonical `--session`; or all three explicit flags `--source`, `--source-path`, `--native-session-id` (Claude, Pi and Codex). Partial/mixed explicit identity flags fail. Pi always requires an exact leaf via `--leaf-id` or runtime `SESSION_INDEX_LEAF_ID`; obtain it from the originating runtime's `getLeafId()`, not the last file entry. An indexed Pi session does not store enough information to guess a leaf. Conflicting runtime/argument leaves fail; non-Pi leaves are unsupported.

The source's native ID and recorded cwd must agree with the requested identity and `--cwd`'s exact Git worktree root, not a common repository directory or project grouping. Missing sources, ambiguous Claude source discovery (set `CLAUDE_TRANSCRIPT_PATH`), unknown IDs, broken Pi ancestry, malformed/incomplete JSONL (including an unfinished final line), missing user/assistant conversation and oversized context fail visibly. `--max-chars` defaults to 80000 and limits the full rendered transcript; nothing is silently truncated. Sources have a 64 MiB read guard. Linear sources use an initial byte-length cutoff; Pi selects only the supplied leaf's ancestry. Appends after capture are not included.

Success fields: `version: 1`, `session_id`, `native_session_id`, `source`, `source_path`, `leaf_id` (null for linear sources), `repo_root`, `branch` (current worktree branch, empty when detached), `captured_at` (UTC ISO timestamp), `source_sha256`, `source_bytes`, `transcript_sha256`, and `transcript` (Markdown). Source hash/length cover the captured source bytes, including alternate Pi branches; transcript hash covers only the returned transcript's UTF-8 bytes. Errors return `{\"error\": {\"code\": \"...\", \"message\": \"...\"}}` and nonzero status. This is conversation intent, not evidence of the staged Git changes.

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
- `side_chats` — parent-owned Side Chat artifacts, not independent sessions. Construct `sidechat/<parent_session_id>/<side_chat_id>`. `headline` is nullable; `first_question` is its routing fallback.
- `question_answers` — one row per asked question. Construct `question/<session_id>/<sequence>/<question_index>`.
- `sessions` — session metadata useful for joins: `session_id`, `project`, `branch`, `started_at`, searchable `summary`, compact `headline`, `substance_band` (`substantial`, `useful`, `low_value`; NULL means unknown), evidence-based `substance_reason`, interaction counts, and generated artifact paths.

### find — compact Evidence Find candidates

```bash
uv run ~/.pi/agent/skills/session-search/scripts/find.py [criteria] [filters]
```

Criteria:

- `--topic TEXT` — session/topic candidates with `session/<session_id>` refs. Exact topic FTS is primary; if exact topic scope is empty, deterministic fuzzy fallback ranks already-indexed session metadata and still honors `--project`, `--since`, `--until`, and `--session`. Terms are AND-joined; `OR` and `NOT` operators work (`--topic "codex OR rollout"`); quoted phrases are not supported. FTS covers user messages, summaries, file paths, and project names, plus Side Chat headlines, Focused Content, questions and answers — not main-session assistant text. Side Chat matches return their parent once with matching child routing labels and refs in `inspect_refs.side_chats`; explicitly inspect a child to read its content. Exact results order by FTS relevance, not recency; fuzzy results order by score. For "most recent session about X", scope with `--since` or check `started_at` rather than trusting the first result.
- `--tool NAME` — Tool Call candidates with `tool/<session_id>/<sequence>` refs.
- `--skill NAME` — Skill Invocation candidates with `skill/<session_id>/<sequence>` refs from `skill_invocations`.
- `--mutated PATH_FRAGMENT` — session-collapsed File Mutation candidates by default, one `session/<session_id>` ref per Canonical Session ID that mutated matching paths.
- `--mutation-mode event` — with `--mutated`, return exact event-level File Mutation rows with `tool/<session_id>/<sequence>` refs.
- `--subagent NAME` — Subagent Run candidates with `subagent/<session_id>/<child_index>` refs and parent-call refs when available.
- `--tool question --question-recommended true|false` — question-answer candidates with question refs.

Filters compose with criteria: `--project`, `--since`, `--until`, `--session`, and `--limit` (default 8). `--skill` does not compose with `--tool` because Skill Invocations are not Tool Calls. `--session` accepts a canonical session ID, a provider-native ID, or an unambiguous 8+ character prefix; unknown sessions and malformed dates return `invalid_find` errors instead of empty results.

`find` emits compact JSON only. Each candidate includes `ref`, `session`, and `match`; `inspect_refs` carries only refs that add information beyond `ref` — the parent-session `context` for event-level candidates, plus criterion extras such as `related_tools`, `tool`, or `parent_call`. `match` omits unused filter fields. `session.summary` is retained because it is high-signal candidate-selection metadata. `find` does not return Evidence Snippets or broad top-level artifact inventories such as repeated Clean Transcript paths, Tool Log paths, or subagent transcript lists. When results fill `--limit`, the payload carries `"truncated": true` (more matches may exist); an empty result set carries a `hint` with the most useful reformulation.

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

- `session/<session_id>` — without `--q`, returns session metadata, generated artifact metadata (including the Clean Transcript artifact path/existence), structured subagent and Side Chat refs, and `evidence: []`; with `--q`, adds query-focused Clean Transcript Evidence Snippets. If the Clean Transcript is not generated yet (typical for still-active or just-ended sessions), the packet returns the session summary plus a `note` instead of an error.
- `skill/<session_id>/<sequence>` — returns Skill Invocation metadata, locator/preview fields, and primary transcript artifact metadata without inlining the full transcript. Parent invocations use the Clean Transcript as primary; subagent-scope invocations use the subagent transcript as primary and include the parent Clean Transcript as context when available.
- `tool/<session_id>/<sequence>` — returns the matching Tool Log section plus associated File Mutation paths.
- `question/<session_id>/<sequence>/<question_index>` — returns question-answer metadata plus the Tool Log section.
- `subagent/<session_id>/<child_index>` — returns task/prompt-area evidence by default; with `--q`, returns query-focused Subagent Run Evidence Snippets.
- `sidechat/<parent_session_id>/<side_chat_id>` — returns bounded opening evidence from the separate Side Chat Transcript; with `--q`, returns query-focused child Evidence Snippets. Parent inspection never inlines the child conversation.

Session inspect artifact metadata has deterministic paths and existence booleans for generated artifacts:

- `artifacts.clean_transcript: {path, exists}`
- `artifacts.tool_log: {path, exists}`
- `artifacts.subagent_transcripts: {count}`
- `artifacts.side_chat_transcripts: {count}`

Session inspect does not expose raw Source Transcript paths and does not list every subagent transcript path. It exposes `inspect_refs.subagents[]` objects with `ref`, `requested_agent_type`, and `task_preview` so you can choose a child run before loading it. `inspect_refs.side_chats[]` includes each child's `ref`, `headline` (or first-question fallback), `opener`, and `started_at`.

`inspect` emits JSON Evidence Packets with artifact path, locator metadata, and bounded Evidence Snippets. Invalid refs, missing sessions, stale refs, and missing artifacts return JSON errors and a non-zero exit status, except session refs with a pending Clean Transcript, which return the summary-plus-`note` packet described above.

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

Use `footprint` when users ask where Session Index disk usage is going or which sessions are safe prune candidates. It reports generated Clean Transcript, Tool Log, Subagent Run, and Side Chat transcript sizes; missing/dangling generated paths; source JSONL retention; fact counts; and prune blockers. It never deletes anything.

### prune — confirmed low-value deletion

```bash
uv run ~/.pi/agent/skills/session-search/scripts/prune.py SESSION_ID [SESSION_ID ...]
uv run ~/.pi/agent/skills/session-search/scripts/prune.py SESSION_ID [SESSION_ID ...] --confirm
```

`prune` is dry-run by default. It deletes only exact Canonical Session IDs supplied on the command line, only when `--confirm` is present, and only when the audit classifies every requested session as low-value. Low-value means the summary has an explicit low-value signal and there are no durable facts for File Mutations, Skill Invocations, Subagent Runs, Side Chats, or question answers. Uncertain cases default to keep. A `low_value` Substance Band is a ranking assessment, not permission to prune; the pruning audit's independent guards still apply. Source JSONL is never deleted.

## Manage sessions

Run `uv run cli.py manage` from the repository, or `uv run ~/.pi/agent/skills/session-search/scripts/manage.py` from any directory.

The full-screen terminal browser shows a compact session list beside the selected session’s preview. In narrower terminals, it stacks the list and preview. It uses readable local dates, a visible selection, and color accents. `NO_COLOR` disables colors.

Results form one continuous list that scrolls a row at a time. Search and filters cover the full inventory. The header shows the search, active filters, and sort order, each next to its key. The list heading shows the selected position and the result count.

Press `/` to enter search terms. Press Enter to apply them or Esc to cancel. Every search term must match somewhere in the indexed headlines, summaries, user messages, project names, file paths, or canonical/native session IDs. Partial words match. If the current filters return no exact matches, the search shows deterministic, typo-tolerant near matches. It also searches Side Chat headlines, Focused Content, questions and answers, but not raw transcripts or main-session assistant messages. The parent preview shows matching excerpts and child headlines/paths, marking matching Side Chats.

Press `f` to open the filter picker. It opens with Project selected. Type part of a project name, then press Enter to apply the filter and return to the session list.

Use Tab or Shift+Tab to switch between these categories:

- project
- local session-start date
- provider
- visibility
- More, which contains Substance Band

The date category includes presets and an inclusive custom range. Unknown substance is separate from Low-value.

Each choice applies immediately, so there is no separate Apply step. Press Esc to cancel the current choice. When you are entering custom dates, press Esc to return to the date choices without applying changes.

An asterisk marks the applied value, while the highlight marks the current candidate. Choose All/Any to clear one category. To clear all filters while keeping the search and sort settings, press Ctrl+R inside the picker.

From the session list, press `?` to open detailed help.

Press `s` to change the sort order. Automatic sorts by newest first while browsing and by best match while searching. You can also choose newest, oldest, best match during search, or substance first. Substance sorting orders sessions by substantial, useful, unknown, and low-value. Within each band, it shows the newest sessions first.

Use these controls:

- ↑/↓ or j/k to select the previous or next session.
- Shift+↑/↓ (or Option/Ctrl+↑/↓, ←/→, p/n) to jump one screen of sessions.
- / to search; f for filters; s for sorting.
- c to reset search, filters, and sorting; ? for keyboard help.
- Tab in the session list to switch between all and hidden sessions, keeping other filters.
- h to hide or unhide a session.
- d to open the deletion confirmation.
- q to quit.
- PgUp/PgDn to scroll the preview.
- r to refresh the list.

Press y to confirm deletion; Esc or n cancels. Run the browser in an interactive terminal at least 60 columns by 24 rows.

- **Hide from recents** preserves all data and search access. It excludes the session from future recent context for the current project, project group, and other projects. The flag survives indexing refreshes. It does not remove context already injected into an open conversation or prevent deliberate retrieval.
- **Delete** requires confirming with y. It removes the database entry, indexed facts, and owned generated artifacts, regardless of the low-value pruning rules. Raw transcripts, shared artifacts, and files outside generated storage remain. There is no undo or deletion exclusion record. Hooks or backfills can re-index preserved raw transcripts. If owned-artifact removal fails, the database entry remains for retry. Files already removed are not restored.

## Transcript storage

Canonical session IDs use `cc:<16-hex>`, `pi:<16-hex>`, or `codex:<16-hex>`; they are not provider-native IDs. Use `current --native` for provider resume/fork commands. Generated text normalizes recognized historical Session Index paths and Inspection References; raw provider logs remain untouched.

Generated artifacts are the normal evidence path:

- `~/.session-index/transcripts/<session-id>.md` — Clean Transcript.
- `~/.session-index/transcripts/<session-id>.tools.md` — Tool Log with ordered tool calls, arguments, status, compact read-only result excerpts, compact large write/edit argument text with hashes, and larger bounded audit excerpts for mutations/errors.
- `~/.session-index/transcripts/<session-id>/agent-*.md` — Subagent Run transcripts.
- `~/.session-index/transcripts/<session-id>/side-chat-<uuid>.md` — Side Chat Transcripts with Focused Content and completed exchanges.

Newly generated or regenerated Clean Transcripts include a compact **Related artifacts** header with absolute paths to the Tool Log and child-transcript directory. Availability reflects files present at generation time. Starting from a copied Clean Transcript path, follow the Tool Log for arguments/results, or list the linked directory's `agent-*.md` files and read selected children for their tasks and final output, including background runs. A missing completion in the parent Tool Log does not mean the child transcript is missing. Older transcripts without this header can be explored with `inspect --ref session/<session-id>` to discover child refs.

Parents with saved Side Chats also list child routing headlines and transcript paths in **Related artifacts**, without embedding child exchanges. Headline generation runs after Side Chat closure; a first-question preview is used until available. Side Chats have no independent session rows, summaries, Substance Bands, or recents entries. They are archived in the parent's raw Pi JSONL as non-context custom entries; archival does not import them into the main agent's context. Previously discarded chats cannot be recovered.

Raw Source JSONL lives at `~/.claude/projects/`, `~/.pi/agent/sessions/`, and Codex rollout files under `~/.codex/sessions/` and `~/.codex/archived_sessions/`.

## When to use this skill

Invoke this skill when the user references past work, asks about prior decisions, wants to audit tool/skill/subagent/question/File Mutation behavior, asks for PR summaries/changelogs from recent work, or needs counts/aggregates across sessions.

Up to seven Top-Level current-project Session Headlines, configured-group sections targeting 14 each, and up to 21 Other projects headlines may already be injected with a shared Clean Transcript root and canonical transcript filenames. Group and Other projects sections use the past seven days, prefer substantial then useful sessions newest first within each band, and treat unknown assessments alongside useful ones. Groups guarantee a representative per active project and may exceed 14 for coverage; low-value sessions are otherwise omitted. Nested Subagent Run sessions and sessions flagged `hidden_from_recents` do not participate. Use this skill when the desired session is absent, or for specific topic lookups, structured audits, and aggregate questions.
