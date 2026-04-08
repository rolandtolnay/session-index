---
name: session-search
description: Search past Claude Code conversations by topic, file, project, or decision
user_invocable: true
arguments:
  - name: query
    description: Search terms, project filter, date range, or combination
    required: false
---

# Session Search

Search and extract content from past Claude Code conversations.

## Commands

### search — Find sessions

```bash
uv run ~/.claude/skills/session-search/scripts/search.py [query] [--project NAME] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--no-any] [--limit N]
```

Returns session summaries: slug, project, date, branch, summary, files touched.

- **query** -- FTS keywords (optional if filters given). Default: OR matching (any term matches)
- **--project** -- prefix match (e.g., `--project synapto` matches synapto-backend, synapto-infra)
- **--since / --until** -- date range filter (ISO dates)
- **--no-any** -- require ALL terms to match (AND). Default is OR
- **--limit** -- max results (default 20)
- Flags combine freely: `search.py "auth token" --project dashboard --since 2026-03-01`

### excerpt — Extract transcript passages

```bash
uv run ~/.claude/skills/session-search/scripts/excerpt.py <session> [<session> ...] -q "keywords"
```

Returns focused transcript blocks from specific sessions (max 3 per call).

- **session** -- slug or session ID from search results (1-3 values)
- **-q / --query** -- keywords to focus extraction (required)
- Example: `excerpt.py fixing-login-bug -q "auth token refresh"`
- Example: `excerpt.py slug1 slug2 -q "migration schema"`

## Workflow

1. **Search first.** Run `search` to find relevant sessions by topic.
2. **Extract if needed.** Copy slug(s) from search results, pass to `excerpt` with keywords.

Most questions are answered by summaries alone. Use `excerpt` only when you need the actual conversation content -- specific decisions, code explanations, or implementation details.

## Query Tips

- Use 1-3 specific terms, not full sentences
- For decisions, use the *topic* not the *action*: `"transcript format"` not `"implemented transcript writer"`
- Browse with `--project` (no query) to orient, then query to narrow

## When to Use

Invoke this skill when the user:
- References past work in another project
- Asks about a prior decision or discussion
- Wants to find or resume a previous conversation (`claude --resume <session_id>`)
- Asks to generate PR summaries or changelogs from recent work

Note: Recent same-project sessions are already in context via the SessionStart hook -- use this skill for older sessions, other projects, or specific topic lookups.
