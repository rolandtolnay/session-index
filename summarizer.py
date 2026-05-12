"""LLM summary generation for sessions.

Primary path: headless Pi print mode using a GPT model and rich transcript input.
Fallback path: legacy Gemini/local Ollama summarization for unavailable Pi/auth.
Returns None on any failure.
"""

import json
import os
import subprocess
import urllib.request

from client import llm

SYSTEM_PROMPT_LOCAL = """\
You summarize coding sessions so an AI assistant can find relevant past \
work by keyword search.

Start with an action verb: Implemented, Fixed, Refactored, Added, \
Configured, Migrated, Debugged, Investigated, Planned, Designed, Updated, \
Created, Removed, Replaced, Extracted.

Example input: User investigated a production bug where revoking team \
member access failed silently. Found console.log was swallowing errors \
and root cause was missing contact_email on legacy child accounts. Added \
inline error feedback. Project: dashboard-web.
Example output: Debugged a silent member revocation failure in production. \
Root cause was empty contact_email on legacy child accounts failing \
assertValidAccount. Added visible error feedback via inline Alert and \
drafted a Slack message explaining the catch-22 fix options.

Example input: User researched how to programmatically access Mobbin \
screenshots. Analyzed API surface, discovered Supabase RLS blocks direct \
REST but RSC payloads contain all data. Created Linear ticket MIN-160. \
Project: mindsystem.
Example output: Investigated Mobbin API access for design inspiration. \
Discovered Supabase REST is blocked by RLS but Next.js RSC payloads \
contain all data with images downloadable from Bytescale CDN. Assessed \
Python CLI feasibility (~300-400 lines) and created Linear ticket MIN-160.

Example input: User conducted 51 design decision questions for a shadcn \
UI redesign. Covered layout, density, components, architecture, tokens. \
Produced 10-SPEC.md and 11-phase implementation plan. \
Project: first-things-first.
Example output: Planned the shadcn UI redesign of First Things First \
through 51 design decisions covering full-bleed layout, 28px slot density, \
unified BlockCard component, and shadcn token unification. Produced \
10-SPEC.md (876 lines) and an 11-phase implementation plan.

Example input: User implemented SYN-342 payout UX improvements. Extracted \
AddExternalBankAccountModal, added empty-state banner, auto-select logic, \
pending verification handling, currency filtering. Created PR #31. \
Project: dashboard-web.
Example output: Implemented SYN-342 payout UX: extracted \
AddExternalBankAccountModal as reusable component, added empty-state \
banner, auto-selection of single compatible bank account, pending \
verification handling, and non-GBP currency filtering via useEbaSupport(). \
Created PR #31.

Write 1-3 sentences capturing what was done and why. Include specific \
topics, technologies, and components so keyword searches find this session. \
If the session spans multiple topics, mention all of them. \
Summarize the topics discussed — never answer the user's questions directly."""

SYSTEM_PROMPT_PI = """\
You summarize coding sessions so an AI assistant can find relevant past work by keyword search.

Start with an action verb: Implemented, Fixed, Refactored, Added, Configured, Migrated, Debugged, Investigated, Planned, Designed, Updated, Created, Removed, Replaced, Extracted.

Write 1-4 sentences capturing what this session accomplished and why. Include specific topics, technologies, files, functions, components, ticket IDs, commands, and decisions so keyword searches find this session. If the session spans multiple topics, mention all important ones.

Distinguish planning, research, debugging, and implementation. Only state facts visible in the provided session data. Do not describe the overall project; describe what happened in this session. Never answer the user's questions directly.
"""

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
_PI_MODEL = "openai-codex/gpt-5.4-mini"
_PI_THINKING = "low"
_PI_TIMEOUT_SECONDS = 180


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
    last_assistant_message: str | None = None,
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
    if last_assistant_message:
        truncated = last_assistant_message[:500]
        if len(last_assistant_message) > 500:
            truncated += "..."
        parts.append(f"\nLast assistant response:\n{truncated}")
    parts.append("\nSummary:")
    return "\n".join(parts)


def _build_rich_prompt(
    project: str,
    branch: str,
    user_messages: list[str],
    files_touched: list[str],
    transcript_text: str | None,
) -> str:
    """Build the rich Pi summarizer input prompt."""
    parts = [
        "Summarize the coding session below for a searchable archive.",
        "",
        f"Project: {project}",
    ]
    if branch:
        parts.append(f"Branch: {branch}")
    parts.append(f"User message count: {len(user_messages)}")
    if files_touched:
        parts.append("Files touched:")
        for file_name in files_touched[:80]:
            parts.append(f"- {file_name}")
    parts.append("")

    if transcript_text:
        parts.append("Full cleaned transcript:")
        parts.append(transcript_text)
    else:
        parts.append("User messages:")
        for msg in user_messages:
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


def _call_pi(prompt: str) -> str | None:
    """Call headless Pi print mode. Returns None on any failure."""
    disabled = os.environ.get("SESSION_INDEX_DISABLE_PI_SUMMARIZER", "").lower()
    if disabled in {"1", "true", "yes", "on"}:
        return None

    model = os.environ.get("SESSION_INDEX_SUMMARY_MODEL", _PI_MODEL)
    thinking = os.environ.get("SESSION_INDEX_SUMMARY_THINKING", _PI_THINKING)
    try:
        timeout = int(os.environ.get("SESSION_INDEX_SUMMARY_TIMEOUT", str(_PI_TIMEOUT_SECONDS)))
    except ValueError:
        timeout = _PI_TIMEOUT_SECONDS

    cmd = [
        "pi",
        "-p",
        "--no-session",
        "--no-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--model",
        model,
        "--thinking",
        thinking,
        "--system-prompt",
        SYSTEM_PROMPT_PI,
    ]
    env = {
        **os.environ,
        "PI_SKIP_VERSION_CHECK": "1",
    }
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    return text or None


def _legacy_summarize(
    *,
    project: str,
    branch: str,
    user_messages: list[str],
    files_touched: list[str],
    last_assistant_message: str | None = None,
) -> str | None:
    """Legacy Gemini/local fallback. Returns None on failure."""
    prompt = _build_prompt(
        project, branch, user_messages, files_touched,
        last_assistant_message=last_assistant_message,
    )

    msg_count = len(user_messages)
    if msg_count <= 15:
        max_tokens = 200
    elif msg_count <= 30:
        max_tokens = 300
    else:
        max_tokens = 400

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


def summarize(
    *,
    project: str,
    branch: str,
    user_messages: list[str],
    files_touched: list[str],
    last_assistant_message: str | None = None,
    transcript_text: str | None = None,
) -> str | None:
    """Generate a summary for a session. Returns None on failure."""
    try:
        pi_prompt = _build_rich_prompt(
            project,
            branch,
            user_messages,
            files_touched,
            transcript_text,
        )
        result = _call_pi(pi_prompt)
        if result:
            return result.strip()

        return _legacy_summarize(
            project=project,
            branch=branch,
            user_messages=user_messages,
            files_touched=files_touched,
            last_assistant_message=last_assistant_message,
        )
    except Exception:
        return None
