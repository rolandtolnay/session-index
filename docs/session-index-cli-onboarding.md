# Session Index CLI: Day-One Onboarding

## What this CLI is for

Session Index is a searchable archive of past AI-agent conversations.

Think of it like an internal analytics and evidence system for agent work. It lets you answer questions such as:

- Which sessions used a certain skill?
- Where did the agent edit files?
- Which questions did I answer against the AI's recommendation?
- Which sessions had lots of bash usage?
- Show me the transcript or tool log for this exact session.

The CLI is not a natural-language chatbot. You translate your question into one of three workflows:

1. **Count, rank, or filter data** → use `query`
2. **Find candidate sessions or events** → use `find`
3. **Inspect the actual evidence** → use `inspect`

That is the core Pareto principle: learn those three commands and you can do most useful audits.

---

## The mental model

There are four important things in Session Index.

### 1. Session

A **session** is one past conversation with Claude Code or Pi.

It has metadata like:

- project
- branch
- start/end time
- summary
- transcript path
- tool log path

Example session ID:

```text
pi:abc123...
```

Pi sessions usually start with `pi:`.

### 2. Clean Transcript

A readable Markdown version of the conversation.

Location pattern:

```text
~/.session-index/transcripts/<session-id>.md
```

Use this when you want to understand what was discussed.

### 3. Tool Log

A detailed Markdown log of tool calls: bash, read, edit, subagents, questions, etc.

Location pattern:

```text
~/.session-index/transcripts/<session-id>.tools.md
```

Use this when you care about exact commands, tool usage, edits, or failures.

### 4. Fact Tables

Session Index also stores structured facts in SQLite tables.

The most important tables are:

- `sessions` — one row per session
- `tool_calls` — one row per tool call
- `file_mutations` — successful writes/edits
- `subagent_runs` — child-agent runs
- `question_answers` — user answers to structured questions

You mostly access these with:

```bash
uv run cli.py query --schema
```

---

## The three-command decision tree

### Use `query` when asking: "How many?", "Which sessions?", "Rank by..."

`query` is for structured analysis.

Examples of questions that need `query`:

- Show sessions with the most bash calls.
- Find sessions where I ignored the recommended answer.
- Which sessions had at least 5 subagent runs?
- Count review-hard usage by project.
- Find all sessions from last week in Development projects.

Basic shape:

```bash
uv run cli.py query "SELECT ... FROM ..." --json
```

Start here to learn the database:

```bash
uv run cli.py query --schema
```

### Use `find` when asking: "Find candidate evidence for..."

`find` gives compact JSON results and inspection references.

It does not dump transcripts. That is intentional.

Examples:

```bash
uv run cli.py find --skill review-hard
uv run cli.py find --tool bash --since 2026-05-25
uv run cli.py find --subagent scout
uv run cli.py find --tool question --question-recommended false
uv run cli.py find --mutated "cli.py"
uv run cli.py find --topic "current session"
```

The key output is a reference like:

```text
session/pi:abc...
tool/pi:abc.../12
subagent/pi:abc.../0
question/pi:abc.../7/0
```

Copy that ref exactly into `inspect`.

### Use `inspect` when asking: "Show me the evidence"

`inspect` turns a reference into bounded evidence text.

Examples:

```bash
uv run cli.py inspect --ref tool/pi:abc/12
uv run cli.py inspect --ref question/pi:abc/7/0
uv run cli.py inspect --ref subagent/pi:abc/0 --q "review"
uv run cli.py inspect --ref session/pi:abc --q "bash"
```

Rule of thumb:

- `find` tells you **where to look**
- `inspect` shows you **what happened there**
- `query` helps you **sort, count, and filter**

---

## The most important schema fields

You do not need to memorize everything. Know these.

### `sessions`

Useful columns:

```text
session_id
source
project
project_path
branch
started_at
ended_at
duration_seconds
summary
transcript_path
tool_log_path
```

Use this table when filtering by project, date, branch, or summary.

### `tool_calls`

Useful columns:

```text
session_id
sequence
timestamp
tool
tool_name
is_error
skill_name
scope
```

Use this table for bash/edit/read/question/skill/tool audits.

Important distinction:

- `tool` is normalized
- `tool_name` is the raw provider name

Usually start with `tool`.

### `question_answers`

Useful columns:

```text
session_id
sequence
question
selected_label
was_recommended
is_other
multi_select
```

Use this for "did I pick the recommended answer?" audits.

Important:

```text
was_recommended = 0
```

means the selected answer did not match the recommended option.

### `subagent_runs`

Useful columns:

```text
parent_session_id
requested_agent_type
child_index
status
tool_call_count
transcript_path
task_preview
```

Use this for subagent audits.

### `file_mutations`

Useful columns:

```text
session_id
sequence
tool
path
```

Use this for actual successful writes/edits.

Do not rely on `sessions.files_touched` for precise mutation audits. That field is broader.

---

## How to approach common audit tasks

These examples are here to teach the method, not to prescribe a single perfect query.

### Example 1: Sessions from the past week where my answer did not match the AI recommendation

Use `query` first because this is a structured filter.

Conceptually:

```sql
SELECT
  'question/' || q.session_id || '/' || q.sequence || '/' || q.question_index AS ref,
  q.session_id,
  s.project,
  s.started_at,
  q.question,
  q.selected_label
FROM question_answers q
JOIN sessions s ON s.session_id = q.session_id
WHERE q.was_recommended = 0
  AND q.multi_select = 0
  AND s.project_path LIKE '%/Documents/Development/%'
  AND s.started_at >= date('now', '-7 days')
ORDER BY s.started_at DESC;
```

Then inspect interesting refs:

```bash
uv run cli.py inspect --ref question/pi:abc/7/0
```

### Example 2: Sessions where the `review-hard` skill produced many review rounds

Likely workflow:

1. Use `find` or `query` to find `review-hard` sessions.
2. Use `inspect` on those tool refs.
3. If "rounds" are not directly structured, inspect transcripts/tool logs and count evidence manually or with a focused query.

Start broad:

```bash
uv run cli.py find --skill review-hard --limit 50
```

Or aggregate:

```sql
SELECT
  t.session_id,
  s.project,
  s.started_at,
  COUNT(*) AS review_hard_calls
FROM tool_calls t
JOIN sessions s ON s.session_id = t.session_id
WHERE t.skill_name = 'review-hard'
GROUP BY t.session_id
ORDER BY s.started_at DESC;
```

Then inspect the candidate tool refs.

### Example 3: Meaningful implementation sessions with many bash calls

Use `query` to rank sessions by bash count.

Conceptually:

```sql
SELECT
  s.session_id,
  s.project,
  s.started_at,
  s.summary,
  COUNT(*) AS bash_calls,
  s.transcript_path,
  s.tool_log_path
FROM tool_calls t
JOIN sessions s ON s.session_id = t.session_id
WHERE t.tool = 'bash'
GROUP BY s.session_id
HAVING bash_calls >= 20
ORDER BY bash_calls DESC;
```

Then inspect a session:

```bash
uv run cli.py inspect --ref session/pi:abc --q "implementation bash"
```

Or inspect specific bash calls:

```bash
uv run cli.py find --tool bash --session pi:abc
uv run cli.py inspect --ref tool/pi:abc/42
```

---

## Day-one operating procedure

For any audit question:

### Step 1: Decide the question type

Ask yourself:

- Do I need counts or rankings? → `query`
- Do I need candidate sessions/events? → `find`
- Do I need actual transcript/tool evidence? → `inspect`

### Step 2: Start with compact results

Avoid opening full transcripts immediately.

Good:

```bash
uv run cli.py find --skill review-hard --limit 20
```

Bad first step:

```bash
cat ~/.session-index/transcripts/huge-session.md
```

### Step 3: Copy refs into `inspect`

If `find` gives you:

```text
tool/pi:abc/12
```

run:

```bash
uv run cli.py inspect --ref tool/pi:abc/12
```

Do not manually derive paths unless `inspect` is insufficient.

### Step 4: Fall back to files only when needed

Generated files are safe fallbacks:

```text
~/.session-index/transcripts/<session-id>.md
~/.session-index/transcripts/<session-id>.tools.md
~/.session-index/transcripts/<session-id>/agent-*.md
```

Prefer these over raw JSONL files.

---

## Cheat sheet

### See database health

```bash
uv run cli.py status
```

### Learn the available tables

```bash
uv run cli.py query --schema
```

### Find sessions by topic

```bash
uv run cli.py find --topic "token refresh" --limit 10
```

### Find tool usage

```bash
uv run cli.py find --tool bash
```

### Find skill usage

```bash
uv run cli.py find --skill review-hard
```

### Find unanswered/non-recommended question choices

```bash
uv run cli.py find --tool question --question-recommended false
```

### Find file edits

```bash
uv run cli.py find --mutated "cli.py"
```

### Inspect a result

```bash
uv run cli.py inspect --ref tool/pi:abc/12
```

### Inspect a whole session around a topic

```bash
uv run cli.py inspect --ref session/pi:abc --q "review"
```

---

## Common mistakes

### Mistake 1: Using transcript search for everything

For audits, structured tables are better.

Use `query` for counts and filters. Use transcripts only after narrowing.

### Mistake 2: Confusing "files touched" with real edits

Use:

```text
file_mutations
```

for successful writes/edits.

Do not use:

```text
sessions.files_touched
```

for exact mutation audits.

### Mistake 3: Reading raw JSONL

Usually avoid raw provider logs.

Prefer:

- Clean Transcript
- Tool Log
- Subagent transcript
- `inspect`

### Mistake 4: Expecting the CLI to understand English

The CLI does not parse natural language.

You translate:

> "sessions where I rejected the recommended option"

into:

```text
question_answers.was_recommended = 0
```

---

## The 20% you must memorize

If you remember only this, you can be productive:

```bash
uv run cli.py query --schema
```

Use this to discover what data exists.

```bash
uv run cli.py query "SELECT ..." --json
```

Use this for audits, counts, rankings, and custom filters.

```bash
uv run cli.py find --skill review-hard
uv run cli.py find --tool bash
uv run cli.py find --tool question --question-recommended false
```

Use this to get candidate refs.

```bash
uv run cli.py inspect --ref <ref>
```

Use this to retrieve the actual evidence.

And remember the core flow:

```text
query/find → get refs → inspect → interpret evidence
```

That is the day-one productivity loop.
