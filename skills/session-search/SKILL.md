---
name: session-search
description: Search past Claude Code conversations by topic, file, project, or decision
user_invocable: true
arguments:
  - name: query
    description: What to search for — a topic, file name, decision, or keyword
    required: true
---

# Session Search

Search the indexed history of past Claude Code conversations.

## Instructions

1. Run the search query against the session index:

```bash
uv run ~/.claude/skills/session-search/scripts/search.py {query}
```

2. Present results to the user in a concise format:
   - **Slug** (or session ID prefix) — clickable identifier
   - **Project** and **branch**
   - **Date** and **duration**
   - **Summary** (or first user message as fallback)
   - **Files touched** (if relevant to the query)

3. If the user wants to dig into a specific session:
   - Read the transcript at `~/.session-index/transcripts/{session_id}.md`
   - Or read the raw JSONL at `~/.claude/projects/{encoded_path}/{session_id}.jsonl`

4. If no results found, suggest:
   - Trying different keywords
   - Running `uv run cli.py backfill` if the index might be incomplete
