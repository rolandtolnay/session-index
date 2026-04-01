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

Search the indexed history of past Claude Code conversations.

## Usage

```bash
uv run ~/.claude/skills/session-search/scripts/search.py [query] [--project NAME] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--excerpt] [--limit N]
```

- **query** -- FTS keywords (optional if filters given)
- **--project** -- prefix match (e.g., `--project synapto` matches synapto-backend, synapto-infra)
- **--since / --until** -- date range filter (ISO dates)
- **--excerpt** -- include matching transcript passages inline (use for context injection)
- Flags combine freely: `search.py "auth token" --project dashboard --since 2026-03-01 --excerpt`

## How to Access Past Conversation Content

**Tier 1 — Search results are enough.** Summaries answer most questions. Use for listing, browsing, or finding a session to resume.

**Tier 2 — Use `--excerpt` for context injection.** When you need specific decisions, explanations, or code from past conversations, add `--excerpt`. This returns matching transcript passages inline — no need to read files separately. Use `--limit 5` for proactive searches to keep output bounded.

**Tier 3 — Grep or Explore agent for deep dives.** Rare. Only when the user asks to broadly scan many sessions (e.g., "browse all Synapto conversations for anything relevant"). Spawn one Explore agent with the session IDs and transcript paths from the search results.

Do NOT read full transcripts into main context. Transcripts live at `~/.session-index/transcripts/{session_id}.md` — use Grep on them if excerpts aren't enough.

## Output Intents

Match your output to the user's intent:
1. **Inject context** -- user references past decisions or prior work. Use `--excerpt --limit 5`. Summarize findings inline.
2. **List/summarize** -- user asks "what did I work on". Present the list with session IDs.
3. **Find resumable session** -- user wants to continue a past conversation. Present: `claude --resume <session_id>`

## Proactive Use

Invoke this skill without being asked when the user:
- References past work in another project ("we decided X in dashboard-web")
- Asks to generate PR summaries or changelogs from recent work
- Wants to find or resume a previous conversation
- Mentions a past decision, discussion, or debugging session

Note: Recent same-project sessions are already in your context via the SessionStart hook — use this skill for older sessions, other projects, or specific topic lookups.
