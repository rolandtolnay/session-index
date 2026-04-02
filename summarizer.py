"""LLM summary generation for sessions.

Short sessions (≤30 messages): local Ollama model with goal-oriented prompt.
Long sessions (30+ messages): Gemini 2.5 Flash Lite with specificity-focused prompt.
Input: project + branch + user messages + files touched (no tool content).
Returns None on any failure.
"""

import json
import os
import urllib.request

from client import llm

SYSTEM_PROMPT_LOCAL = """\
You summarize coding sessions for a searchable index. Future AI assistants \
will search these summaries to find relevant past work. Write 1-3 sentences \
capturing what was done and why, so the right session surfaces when someone \
searches for the topic. Include the specific topics, technologies, and \
questions the user raised so searches for those terms find this session. \
If the session spans multiple topics, mention all of them.

Example input: User asked to fix login timeout. Modified auth/session.ts \
and tests. Branch: fix/login-timeout.
Example output: Fixed session timeout bug by increasing token TTL from 15m \
to 1h and adding automatic refresh before expiry.

Example input: User asked to add dark mode toggle. Created ThemeProvider, \
modified App.tsx and settings page. Branch: feat/dark-mode.
Example output: Implemented dark mode with a ThemeProvider context, CSS \
variables for color tokens, and a toggle in user settings that persists \
to localStorage."""

SYSTEM_PROMPT_GEMINI = """\
Summarize this coding session for a searchable archive. Another AI will \
read your summary to decide if this session is relevant to a future question.

Write 2-4 sentences that answer: What was built or decided? What specific \
components, APIs, or files were changed? What was the outcome?

Rules:
- Name specific components, functions, tickets, and files — these are \
search keywords
- Distinguish planning from implementation: if the output was a plan, \
spec, or design doc, say "Planned/Designed X"; if code was written and \
committed, say "Implemented/Built X"
- Only state facts visible in the messages below — never infer errors, \
bugs, or outcomes not explicitly mentioned
- If the session spans multiple topics, mention all of them
- Do not describe the project — describe what THIS SESSION accomplished"""

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)
_GEMINI_MODEL = "gemini-2.5-flash-lite"
_LONG_SESSION_THRESHOLD = 30


def _select_messages(msgs: list[str], budget: int = 30) -> list[str]:
    """Select representative messages: first 5 + last 5 + evenly sampled middle."""
    if len(msgs) <= budget:
        return msgs
    first = msgs[:5]
    last = msgs[-5:]
    middle = msgs[5:-5]
    step = max(1, len(middle) // (budget - 10))
    sampled = [middle[i] for i in range(0, len(middle), step)][:budget - 10]
    return first + sampled + last


def _build_prompt(
    project: str,
    branch: str,
    user_messages: list[str],
    files_touched: list[str],
) -> str:
    """Build the summarizer input prompt."""
    parts = [f"Project: {project}"]
    if branch:
        parts.append(f"Branch: {branch}")
    if files_touched:
        parts.append(f"Files: {', '.join(files_touched[:20])}")
    parts.append("")
    parts.append("User messages:")
    for i, msg in enumerate(_select_messages(user_messages)):
        # First message often contains the task/plan — allow more context
        budget = 2000 if i == 0 else 500
        if len(msg) > budget:
            msg = msg[:budget] + "..."
        parts.append(f"- {msg}")
    parts.append("\nSummary:")
    return "\n".join(parts)


def _call_gemini(prompt: str, max_tokens: int) -> str | None:
    """Call Gemini 2.5 Flash Lite. Returns None on any failure."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    body = json.dumps({
        "model": _GEMINI_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_GEMINI},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(
        _GEMINI_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return text.strip() or None


def summarize(
    *,
    project: str,
    branch: str,
    user_messages: list[str],
    files_touched: list[str],
) -> str | None:
    """Generate a summary for a session. Returns None on failure."""
    try:
        prompt = _build_prompt(project, branch, user_messages, files_touched)

        msg_count = len(user_messages)
        if msg_count <= 15:
            max_tokens = 200
        elif msg_count <= 30:
            max_tokens = 300
        else:
            max_tokens = 400

        # Long sessions: try Gemini Flash Lite first, fall back to local
        if msg_count > _LONG_SESSION_THRESHOLD:
            result = _call_gemini(prompt, max_tokens)
            if result:
                return result

        result = llm(
            prompt,
            system=SYSTEM_PROMPT_LOCAL,
            temperature=0.1,
            max_tokens=max_tokens,
            think=False,
            timeout=30,
        )
        return result.strip() if result and result.strip() else None
    except Exception:
        return None
