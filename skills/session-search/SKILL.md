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
uv run ~/.claude/skills/session-search/scripts/search.py [query] [--project NAME] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--limit N]
```

- **query** -- FTS keywords (optional if filters given)
- **--project** -- prefix match (e.g., `--project synapto` matches synapto-backend, synapto-infra)
- **--since / --until** -- date range filter (ISO dates)
- Flags combine freely: `search.py "auth token" --project dashboard --since 2026-03-01`

## Output Intents

Match your output to the user's intent:
1. **Inject context** -- user references past decisions or prior work. Summarize relevant results inline.
2. **List/summarize** -- user asks "what did I work on". Present the list with session IDs.
3. **Find resumable session** -- user wants to continue a past conversation. Present: `claude --resume <session_id>`

## Transcripts

For deeper detail on a specific session, read the transcript:
`~/.session-index/transcripts/{session_id}.md`

Transcript format: `[user] ---...` / `[assistant] ---...` delimit messages. `[/skill-name] args` marks skill invocations.

## Proactive Use

Invoke this skill without being asked when the user:
- References past work in another project ("we decided X in dashboard-web")
- Asks to generate PR summaries or changelogs from recent work
- Wants to find or resume a previous conversation
- Mentions a past decision, discussion, or debugging session
