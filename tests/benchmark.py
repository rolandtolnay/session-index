#!/usr/bin/env python3
# /// script
# dependencies = []
# ///
"""Benchmark harness for session summary quality testing.

Generates summaries for given sessions across multiple configurations,
supporting both local Ollama and Gemini Flash backends.

Usage:
    uv run tests/benchmark.py --select-sessions
    uv run tests/benchmark.py --sessions <id1,id2,...> --configs A,B,C,D,E,F --output results.json
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
LOCAL_MODEL = "qwen3.5:4b"
GEMINI_MODEL = "gemini-2.5-flash-lite"

SYSTEM_PROMPT = """\
You summarize coding sessions for a searchable index. Future AI assistants \
will search these summaries to find relevant past work. Write 1-3 sentences \
capturing what was done and why, so the right session surfaces when someone \
searches for the topic.

Example input: User asked to fix login timeout. Modified auth/session.ts \
and tests. Branch: fix/login-timeout.
Example output: Fixed session timeout bug by increasing token TTL from 15m \
to 1h and adding automatic refresh before expiry.

Example input: User asked to add dark mode toggle. Created ThemeProvider, \
modified App.tsx and settings page. Branch: feat/dark-mode.
Example output: Implemented dark mode with a ThemeProvider context, CSS \
variables for color tokens, and a toggle in user settings that persists \
to localStorage.\
"""

# ── Config definitions ──────────────────────────────────────────────────

CONFIGS = {
    "A": {"first_msg_budget": 500,  "max_tokens": "fixed",  "backend": "local"},
    "B": {"first_msg_budget": 2000, "max_tokens": "fixed",  "backend": "local"},
    "C": {"first_msg_budget": 500,  "max_tokens": "scaled", "backend": "local"},
    "D": {"first_msg_budget": 2000, "max_tokens": "scaled", "backend": "local"},
    "E": {"first_msg_budget": 500,  "max_tokens": "scaled", "backend": "gemini"},
    "F": {"first_msg_budget": 2000, "max_tokens": "scaled", "backend": "gemini"},
}


def get_max_tokens(msg_count: int, mode: str) -> int:
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
    first_msg_budget: int,
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

def call_local(prompt: str, max_tokens: int) -> tuple[str, float]:
    """Call local Ollama model."""
    body = json.dumps({
        "model": LOCAL_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
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
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    elapsed = time.time() - t0
    text = data.get("message", {}).get("content", "")
    return text.strip(), elapsed


def call_gemini(prompt: str, max_tokens: int) -> tuple[str, float]:
    """Call Gemini 2.5 Flash via OpenAI-compatible API."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return "[ERROR: GEMINI_API_KEY not set]", 0.0
    body = json.dumps({
        "model": GEMINI_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
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


# ── Main logic ──────────────────────────────────────────────────────────

def run_benchmark(session_id: str, config_name: str) -> dict:
    """Run a single benchmark: session x config -> summary."""
    config = CONFIGS[config_name]
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT project, branch, user_messages, files_touched, "
        "user_message_count, summary FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    conn.close()
    if not row:
        return {"error": f"Session {session_id} not found"}

    project, branch, user_messages_raw, files_raw, msg_count, original = row
    user_messages = user_messages_raw.split("\n---\n") if user_messages_raw else []
    files = files_raw.split(",") if files_raw else []

    prompt = build_prompt(
        project=project or "",
        branch=branch or "",
        user_messages=user_messages,
        files_touched=[f.strip() for f in files],
        first_msg_budget=config["first_msg_budget"],
    )

    max_tokens = get_max_tokens(msg_count or len(user_messages), config["max_tokens"])

    if config["backend"] == "local":
        summary, elapsed = call_local(prompt, max_tokens)
    else:
        summary, elapsed = call_gemini(prompt, max_tokens)

    return {
        "session_id": session_id,
        "config": config_name,
        "project": project,
        "msg_count": msg_count,
        "max_tokens_used": max_tokens,
        "backend": config["backend"],
        "first_msg_budget": config["first_msg_budget"],
        "summary": summary,
        "original_summary": original,
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

    # Sessions with compaction markers
    tx_dir = os.path.expanduser("~/.session-index/transcripts")
    compacted = []
    if os.path.isdir(tx_dir):
        for fname in os.listdir(tx_dir):
            if not fname.endswith(".md"):
                continue
            path = os.path.join(tx_dir, fname)
            try:
                with open(path, "r") as f:
                    content = f.read(200_000)  # first 200k chars
                if "continued from a previous conversation" in content:
                    sid = fname.replace(".md", "")
                    row = conn.execute(
                        "SELECT project, user_message_count FROM sessions "
                        "WHERE session_id = ? AND summary IS NOT NULL", (sid,),
                    ).fetchone()
                    if row:
                        compacted.append({"id": sid, "project": row[0], "msgs": row[1]})
            except OSError:
                continue
    result["compacted"] = compacted

    conn.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Session summary benchmark")
    parser.add_argument("--select-sessions", action="store_true",
                        help="Print recommended test sessions")
    parser.add_argument("--sessions", help="Comma-separated session IDs")
    parser.add_argument("--configs", default="A,B,C,D,E,F",
                        help="Configs to test (default: all)")
    parser.add_argument("--output", default="-",
                        help="Output file path (- for stdout)")
    args = parser.parse_args()

    if args.select_sessions:
        sessions = select_test_sessions()
        print(json.dumps(sessions, indent=2))
        sys.exit(0)

    if not args.sessions:
        parser.error("--sessions is required (or use --select-sessions)")

    session_ids = [s.strip() for s in args.sessions.split(",")]
    configs = [c.strip() for c in args.configs.split(",")]

    results = []
    total = len(session_ids) * len(configs)
    for i, sid in enumerate(session_ids):
        for j, cfg in enumerate(configs):
            idx = i * len(configs) + j + 1
            print(f"[{idx}/{total}] {sid[:8]}… config {cfg}", file=sys.stderr)
            try:
                result = run_benchmark(sid, cfg)
            except Exception as e:
                result = {
                    "session_id": sid, "config": cfg,
                    "error": str(e),
                }
            results.append(result)

    output = json.dumps(results, indent=2, ensure_ascii=False)
    if args.output == "-":
        print(output)
    else:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Results written to {args.output}", file=sys.stderr)
