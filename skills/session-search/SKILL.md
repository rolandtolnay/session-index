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

## Workflow

1. **Search first.** Run `search` to find relevant sessions by topic.
2. **Extract if needed.** Copy session ID(s) from search results, pass to `excerpt` with keywords.
3. **Fall back to reading the cleaned transcript directly** if `excerpt` returns off-topic blocks after one query refinement, or when the footer reports more agent-transcript matches you want to see.

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

Note: Recent same-project sessions are already injected automatically when the relevant Claude hook or Pi extension is installed. Use this skill for older sessions, other projects, or specific topic lookups.
