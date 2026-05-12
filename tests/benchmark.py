#!/usr/bin/env python3
# /// script
# dependencies = []
# ///
"""Benchmark harness for session summary quality testing.

Supports two benchmark modes:
  1. Config mode (--configs): Tests input/output configurations (first_msg_budget,
     max_tokens scaling, backend). Used in Round 1 to find optimal settings.
  2. Prompt mode (--prompts): Tests system prompt variants with fixed Config D
     settings. Used in Round 2+ to optimize prompt wording.

The --model flag selects the Ollama model (default: gemma4:e4b).

Usage:
    uv run tests/benchmark.py --select-sessions
    uv run tests/benchmark.py --sessions <ids> --prompts A,B,C,D,E,F --output results.json
    uv run tests/benchmark.py --sessions <ids> --configs A,B,C,D --model qwen3.5:4b --output results.json
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request

DB_PATH = os.path.expanduser("~/.session-index/sessions.db")
OLLAMA_URL = "http://localhost:11434/api/chat"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL = "gemini-2.5-flash-lite"

DEFAULT_MODEL = "gemma4:e4b"
FIRST_MSG_BUDGET = 2000

# ── System prompts ────────────────────────────────────────────────────

BASELINE_SYSTEM_PROMPT = """\
You summarize coding sessions for a searchable index. Future AI assistants \
will search these summaries to find relevant past work. Write 1-3 sentences \
capturing what was done and why, so the right session surfaces when someone \
searches for the topic. Include the specific topics, technologies, and \
questions the user raised so searches for those terms find this session. \
If the session spans multiple topics, mention all of them. \
Summarize the topics discussed — never answer the user's questions directly.

Example input: User asked to fix login timeout. Modified auth/session.ts \
and tests. Branch: fix/login-timeout.
Example output: Fixed session timeout bug by increasing token TTL from 15m \
to 1h and adding automatic refresh before expiry.

Example input: User asked to add dark mode toggle. Created ThemeProvider, \
modified App.tsx and settings page. Branch: feat/dark-mode.
Example output: Implemented dark mode with a ThemeProvider context, CSS \
variables for color tokens, and a toggle in user settings that persists \
to localStorage."""

GEMINI_SYSTEM_PROMPT = """\
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

# ── Prompt variants (Round 2: prompt A/B testing) ─────────────────────

PROMPT_VARIANTS = {
    "A": BASELINE_SYSTEM_PROMPT,

    "B": """\
You summarize coding sessions so an AI assistant can find relevant past \
work by keyword search.

Write 1-3 sentences capturing what was done and why, so the right session \
surfaces when someone searches for the topic. Include the specific topics, \
technologies, and questions the user raised so searches for those terms \
find this session. If the session spans multiple topics, mention all of them. \
Summarize the topics discussed — never answer the user's questions directly.

Example input: User asked to fix login timeout. Modified auth/session.ts \
and tests. Branch: fix/login-timeout.
Example output: Fixed session timeout bug by increasing token TTL from 15m \
to 1h and adding automatic refresh before expiry.

Example input: User asked to add dark mode toggle. Created ThemeProvider, \
modified App.tsx and settings page. Branch: feat/dark-mode.
Example output: Implemented dark mode with a ThemeProvider context, CSS \
variables for color tokens, and a toggle in user settings that persists \
to localStorage.""",

    "C": """\
You summarize coding sessions for a searchable index. Future AI assistants \
will search these summaries to find relevant past work.

Example input: User asked to fix login timeout. Modified auth/session.ts \
and tests. Branch: fix/login-timeout.
Example output: Fixed session timeout bug by increasing token TTL from 15m \
to 1h and adding automatic refresh before expiry.

Example input: User asked to add dark mode toggle. Created ThemeProvider, \
modified App.tsx and settings page. Branch: feat/dark-mode.
Example output: Implemented dark mode with a ThemeProvider context, CSS \
variables for color tokens, and a toggle in user settings that persists \
to localStorage.

Write 1-3 sentences capturing what was done and why. Include specific \
topics, technologies, and components so keyword searches find this session. \
If the session spans multiple topics, mention all of them. \
Summarize the topics discussed — never answer the user's questions directly.""",

    "D": """\
You summarize coding sessions for a searchable index. Future AI assistants \
will search these summaries to find relevant past work. Write 1-3 sentences \
capturing what was done and why, so the right session surfaces when someone \
searches for the topic. Include the specific topics, technologies, and \
questions the user raised so searches for those terms find this session. \
If the session spans multiple topics, mention all of them. \
Summarize the topics discussed — never answer the user's questions directly.

Start with an action verb: Implemented, Fixed, Refactored, Added, \
Configured, Migrated, Debugged, Investigated, Planned, Designed, Updated, \
Created, Removed, Replaced, Extracted.

Example input: User asked to fix login timeout. Modified auth/session.ts \
and tests. Branch: fix/login-timeout.
Example output: Fixed session timeout bug by increasing token TTL from 15m \
to 1h and adding automatic refresh before expiry.

Example input: User asked to add dark mode toggle. Created ThemeProvider, \
modified App.tsx and settings page. Branch: feat/dark-mode.
Example output: Implemented dark mode with a ThemeProvider context, CSS \
variables for color tokens, and a toggle in user settings that persists \
to localStorage.""",

    "E": """\
You summarize coding sessions for a searchable index. Future AI assistants \
will search these summaries to find relevant past work. Write 1-3 sentences \
capturing what was done and why, so the right session surfaces when someone \
searches for the topic. Include the specific topics, technologies, and \
questions the user raised so searches for those terms find this session. \
If the session spans multiple topics, mention all of them. \
Summarize the topics discussed — never answer the user's questions directly.

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
Created PR #31.""",

    "F": """\
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
Summarize the topics discussed — never answer the user's questions directly.""",
}

# ── Config variants (Round 1: input/output config testing) ────────────

CONFIG_VARIANTS = {
    "A": {"first_msg_budget": 500,  "max_tokens": "fixed",  "backend": "local"},
    "B": {"first_msg_budget": 2000, "max_tokens": "fixed",  "backend": "local"},
    "C": {"first_msg_budget": 500,  "max_tokens": "scaled", "backend": "local"},
    "D": {"first_msg_budget": 2000, "max_tokens": "scaled", "backend": "local"},
    "E": {"first_msg_budget": 500,  "max_tokens": "scaled", "backend": "gemini"},
    "F": {"first_msg_budget": 2000, "max_tokens": "scaled", "backend": "gemini"},
}


def get_max_tokens(msg_count: int, mode: str = "scaled") -> int:
    """Return max output tokens based on session length."""
    if mode == "fixed":
        return 200
    if msg_count <= 15:
        return 200
    elif msg_count <= 30:
        return 300
    else:
        return 400


def select_messages(msgs: list[str], budget: int = 30) -> list[str]:
    """Select representative messages: first 5 + last 5 + evenly sampled middle."""
    if len(msgs) <= budget:
        return msgs
    first = msgs[:5]
    last = msgs[-5:]
    middle = msgs[5:-5]
    step = max(1, len(middle) // (budget - 10))
    sampled = [middle[i] for i in range(0, len(middle), step)][: budget - 10]
    return first + sampled + last


def build_prompt(
    project: str,
    branch: str,
    user_messages: list[str],
    files_touched: list[str],
    first_msg_budget: int = FIRST_MSG_BUDGET,
) -> str:
    """Build the summarizer input prompt."""
    parts = [f"Project: {project}"]
    if branch:
        parts.append(f"Branch: {branch}")
    if files_touched:
        parts.append(f"Files: {', '.join(files_touched[:20])}")
    parts.append("")
    parts.append("User messages:")
    for i, msg in enumerate(select_messages(user_messages)):
        budget = first_msg_budget if i == 0 else 500
        if len(msg) > budget:
            msg = msg[:budget] + "..."
        parts.append(f"- {msg}")
    parts.append("\nSummary:")
    return "\n".join(parts)


# ── Backends ────────────────────────────────────────────────────────────

def call_local(prompt: str, system_prompt: str, max_tokens: int, model: str) -> tuple[str, float]:
    """Call local Ollama model with given system prompt."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "keep_alive": -1,
        "options": {
            "temperature": 0.1,
            "num_predict": max_tokens,
            "num_ctx": 8192,
        },
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    elapsed = time.time() - t0
    text = data.get("message", {}).get("content", "")
    return text.strip(), elapsed


def call_gemini(prompt: str, system_prompt: str, max_tokens: int) -> tuple[str, float]:
    """Call Gemini via OpenAI-compatible API."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return "[ERROR: GEMINI_API_KEY not set]", 0.0
    body = json.dumps({
        "model": GEMINI_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(
        GEMINI_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    elapsed = time.time() - t0
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return text.strip(), elapsed


# ── Session loading ────────────────────────────────────────────────────

def load_session(session_id: str) -> dict | None:
    """Load session data from DB. Returns None if not found."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT project, branch, user_messages, files_touched, "
        "user_message_count, summary FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    project, branch, user_messages_raw, files_raw, msg_count, original = row
    return {
        "project": project or "",
        "branch": branch or "",
        "user_messages": user_messages_raw.split("\n---\n") if user_messages_raw else [],
        "files_touched": [f.strip() for f in files_raw.split(",")] if files_raw else [],
        "msg_count": msg_count or 0,
        "original_summary": original,
    }


# ── Benchmark runners ─────────────────────────────────────────────────

def run_prompt_benchmark(session_id: str, prompt_name: str, model: str) -> dict:
    """Run prompt-mode benchmark: fixed Config D, varying system prompt."""
    session = load_session(session_id)
    if not session:
        return {"error": f"Session {session_id} not found"}

    prompt = build_prompt(
        project=session["project"],
        branch=session["branch"],
        user_messages=session["user_messages"],
        files_touched=session["files_touched"],
    )
    max_tokens = get_max_tokens(session["msg_count"])
    system_prompt = PROMPT_VARIANTS[prompt_name]
    summary, elapsed = call_local(prompt, system_prompt, max_tokens, model)

    return {
        "session_id": session_id,
        "prompt": prompt_name,
        "model": model,
        "project": session["project"],
        "msg_count": session["msg_count"],
        "max_tokens_used": max_tokens,
        "summary": summary,
        "original_summary": session["original_summary"],
        "elapsed_s": round(elapsed, 2),
    }


def run_config_benchmark(session_id: str, config_name: str, model: str) -> dict:
    """Run config-mode benchmark: varying input/output settings."""
    config = CONFIG_VARIANTS[config_name]
    session = load_session(session_id)
    if not session:
        return {"error": f"Session {session_id} not found"}

    prompt = build_prompt(
        project=session["project"],
        branch=session["branch"],
        user_messages=session["user_messages"],
        files_touched=session["files_touched"],
        first_msg_budget=config["first_msg_budget"],
    )
    max_tokens = get_max_tokens(session["msg_count"], config["max_tokens"])

    if config["backend"] == "local":
        summary, elapsed = call_local(prompt, BASELINE_SYSTEM_PROMPT, max_tokens, model)
    else:
        summary, elapsed = call_gemini(prompt, GEMINI_SYSTEM_PROMPT, max_tokens)

    return {
        "session_id": session_id,
        "config": config_name,
        "model": model if config["backend"] == "local" else "gemini-2.5-flash-lite",
        "project": session["project"],
        "msg_count": session["msg_count"],
        "max_tokens_used": max_tokens,
        "backend": config["backend"],
        "first_msg_budget": config["first_msg_budget"],
        "summary": summary,
        "original_summary": session["original_summary"],
        "elapsed_s": round(elapsed, 2),
    }


def select_test_sessions() -> dict:
    """Select test sessions across buckets."""
    conn = sqlite3.connect(DB_PATH)
    result = {}

    for label, lo, hi in [
        ("short", 3, 15),
        ("medium", 16, 30),
        ("long", 31, 999),
    ]:
        rows = conn.execute(
            "SELECT session_id, project, branch, user_message_count, "
            "length(summary) as slen "
            "FROM sessions WHERE summary IS NOT NULL "
            "AND user_message_count BETWEEN ? AND ? "
            "ORDER BY user_message_count DESC",
            (lo, hi),
        ).fetchall()
        result[label] = [
            {"id": r[0], "project": r[1], "branch": r[2], "msgs": r[3], "summary_len": r[4]}
            for r in rows
        ]

    conn.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Session summary benchmark")
    parser.add_argument("--select-sessions", action="store_true",
                        help="Print recommended test sessions")
    parser.add_argument("--sessions", help="Comma-separated session IDs")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Ollama model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--output", default="-",
                        help="Output file path (- for stdout)")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prompts",
                      help="Prompt variants to test (e.g. A,B,C,D,E,F)")
    mode.add_argument("--configs",
                      help="Config variants to test (e.g. A,B,C,D,E,F)")

    args = parser.parse_args()

    if args.select_sessions:
        sessions = select_test_sessions()
        print(json.dumps(sessions, indent=2))
        sys.exit(0)

    if not args.sessions:
        parser.error("--sessions is required (or use --select-sessions)")
    if not args.prompts and not args.configs:
        parser.error("Either --prompts or --configs is required")

    session_ids = [s.strip() for s in args.sessions.split(",")]

    if args.prompts:
        variant_names = [p.strip() for p in args.prompts.split(",")]
        for p in variant_names:
            if p not in PROMPT_VARIANTS:
                parser.error(f"Unknown prompt variant: {p} (available: {','.join(PROMPT_VARIANTS)})")
        run_fn = lambda sid, name: run_prompt_benchmark(sid, name, args.model)
        label_key = "prompt"
    else:
        variant_names = [c.strip() for c in args.configs.split(",")]
        for c in variant_names:
            if c not in CONFIG_VARIANTS:
                parser.error(f"Unknown config variant: {c} (available: {','.join(CONFIG_VARIANTS)})")
        run_fn = lambda sid, name: run_config_benchmark(sid, name, args.model)
        label_key = "config"

    results = []
    total = len(session_ids) * len(variant_names)
    for i, sid in enumerate(session_ids):
        for j, vname in enumerate(variant_names):
            idx = i * len(variant_names) + j + 1
            print(f"[{idx}/{total}] {sid[:8]}… {label_key} {vname}", file=sys.stderr)
            try:
                result = run_fn(sid, vname)
            except Exception as e:
                result = {
                    "session_id": sid, label_key: vname,
                    "error": str(e),
                }
            results.append(result)

    output = json.dumps(results, indent=2, ensure_ascii=False)
    if args.output == "-":
        print(output)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Results written to {args.output}", file=sys.stderr)
