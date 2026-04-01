"""LLM summary generation for sessions.

Uses the local Ollama model with a goal-oriented system prompt.
Input: project + branch + user messages + files touched (no tool content).
Returns None on any failure.
"""

from client import llm

SYSTEM_PROMPT = """\
You summarize coding sessions for a searchable index. Future AI assistants will search these summaries to find relevant past work. Write 1-3 sentences capturing what was done and why, so the right session surfaces when someone searches for the topic.

Example input: User asked to fix login timeout. Modified auth/session.ts and tests. Branch: fix/login-timeout.
Example output: Fixed session timeout bug by increasing token TTL from 15m to 1h and adding automatic refresh before expiry.

Example input: User asked to add dark mode toggle. Created ThemeProvider, modified App.tsx and settings page. Branch: feat/dark-mode.
Example output: Implemented dark mode with a ThemeProvider context, CSS variables for color tokens, and a toggle in user settings that persists to localStorage.
"""


def summarize(
    *,
    project: str,
    branch: str,
    user_messages: list[str],
    files_touched: list[str],
) -> str | None:
    """Generate a summary for a session. Returns None on failure."""
    try:
        parts = [f"Project: {project}"]
        if branch:
            parts.append(f"Branch: {branch}")
        if files_touched:
            parts.append(f"Files: {', '.join(files_touched[:20])}")
        parts.append("")
        parts.append("User messages:")
        for msg in user_messages[:30]:
            # Truncate very long messages
            if len(msg) > 500:
                msg = msg[:500] + "..."
            parts.append(f"- {msg}")

        parts.append("\nSummary:")
        prompt = "\n".join(parts)

        result = llm(
            prompt,
            system=SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=200,
            think=False,
            timeout=30,
        )
        return result.strip() if result and result.strip() else None
    except Exception:
        return None
