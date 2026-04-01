#!/usr/bin/env python3
"""Detached worker — re-parses JSONL, generates LLM summary, writes transcript, updates DB.

Launched by session_end.py as a detached subprocess.
If LLM fails: row keeps NULL summary, backfill can repair later.

Usage: python3 _session_end_worker.py <session_id> <cwd>
"""

import os
import subprocess
import sys
import time

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log


def main() -> None:
    if len(sys.argv) < 3:
        return

    session_id = sys.argv[1]
    cwd = sys.argv[2]
    start = time.monotonic()

    log(session_id, "worker", "started")

    # Derive JSONL path
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        project_root = result.stdout.strip() if result.returncode == 0 else cwd
    except Exception:
        project_root = cwd

    encoded = "-" + project_root.replace("/", "-")
    projects_dir = os.path.expanduser("~/.claude/projects")
    jsonl_path = os.path.join(projects_dir, encoded, f"{session_id}.jsonl")

    if not os.path.exists(jsonl_path):
        log(session_id, "worker", f"jsonl not found: {jsonl_path}")
        return

    from parser import parse_jsonl
    from db import init_db, get_connection, upsert_session
    from summarizer import summarize
    from transcript import write_transcript

    session = parse_jsonl(jsonl_path)

    if session.user_message_count < 3:
        log(session_id, "worker", f"skipped ({session.user_message_count} user msgs)")
        return

    # Generate summary (may return None if Ollama is down)
    summary = summarize(
        project=session.project,
        branch=session.branch,
        user_messages=session.user_messages,
        files_touched=session.files_touched,
    )

    if summary:
        log(session_id, "worker", f"summary generated ({len(summary)} chars)")
    else:
        log(session_id, "worker", "summary failed (LLM unavailable)")

    # Write transcript
    transcript_path = None
    if session.messages:
        transcript_path = write_transcript(
            session.session_id,
            session.messages,
            slug=session.slug,
            project=session.project,
            branch=session.branch,
            timestamp=session.started_at,
        )
        log(session_id, "worker", f"transcript written")

    # Update DB with summary and transcript
    conn = get_connection()
    init_db(conn)

    upsert_session(
        conn,
        session_id=session.session_id,
        slug=session.slug or None,
        project_path=session.project_path or None,
        project=session.project or None,
        branch=session.branch or None,
        model=session.model or None,
        started_at=session.started_at or None,
        ended_at=session.ended_at or None,
        duration_seconds=session.duration_seconds or None,
        user_message_count=session.user_message_count,
        user_messages="\n---\n".join(session.user_messages) if session.user_messages else None,
        files_touched=", ".join(session.files_touched) if session.files_touched else None,
        tools_used=session.tools_used or None,
        summary=summary,
        transcript_path=transcript_path,
    )
    conn.close()

    elapsed = time.monotonic() - start
    log(session_id, "worker", f"done ({elapsed:.1f}s)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            sid = sys.argv[1] if len(sys.argv) > 1 else "??????"
            log(sid, "worker", f"error: {e}")
        except Exception:
            pass
