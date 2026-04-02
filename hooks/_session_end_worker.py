#!/usr/bin/env python3
"""Detached worker — re-parses JSONL, generates LLM summary, writes transcript, updates DB.

Launched by session_end.py as a detached subprocess.
If LLM fails: row keeps NULL summary, backfill can repair later.

Usage: python3 _session_end_worker.py <session_id> <jsonl_path>
"""

import os
import sys
import time

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log


def main() -> None:
    if len(sys.argv) < 3:
        return

    session_id = sys.argv[1]
    jsonl_path = sys.argv[2]
    start = time.monotonic()

    log(session_id, "worker", "started")

    if not os.path.exists(jsonl_path):
        log(session_id, "worker", f"jsonl not found: {jsonl_path}")
        return

    from parser import parse_jsonl, clean_user_messages
    from db import init_db, get_connection, upsert_session
    from summarizer import summarize
    from transcript import write_transcript, write_subagent_transcript
    from subagent_parser import discover_subagents, parse_subagent_jsonl

    session = parse_jsonl(jsonl_path)

    if session.user_message_count < 1 or session.assistant_message_count < 1:
        log(session_id, "worker", f"skipped ({session.user_message_count} user, {session.assistant_message_count} assistant msgs)")
        return

    # Discover and parse subagents
    subagent_infos = discover_subagents(jsonl_path)
    parsed_subagents = []
    for info in subagent_infos:
        parsed = parse_subagent_jsonl(info.jsonl_path, info.meta_path)
        if parsed.messages:
            parsed_subagents.append(parsed)

    # Aggregate subagent files_touched into parent
    all_files = set(session.files_touched)
    for sub in parsed_subagents:
        all_files.update(sub.files_touched)
    enriched_files = sorted(all_files)

    if parsed_subagents:
        log(session_id, "worker", f"found {len(parsed_subagents)} subagent(s)")

    # For short sessions, include last assistant message to capture outcomes
    # that aren't visible from user messages alone (e.g. Q&A sessions)
    SHORT_SESSION_THRESHOLD = 5
    last_assistant = None
    if session.user_message_count <= SHORT_SESSION_THRESHOLD and session.assistant_messages:
        last_assistant = session.assistant_messages[-1]

    # Generate summary (may return None if Ollama is down)
    summary = summarize(
        project=session.project,
        branch=session.branch,
        user_messages=clean_user_messages(session.user_messages),
        files_touched=session.files_touched,
        last_assistant_message=last_assistant,
    )

    if summary:
        log(session_id, "worker", f"summary generated ({len(summary)} chars)")
    else:
        log(session_id, "worker", "summary failed (LLM unavailable)")

    # Write parent transcript (now includes inline subagent markers from parse_jsonl)
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

    # Write subagent transcripts
    subagent_paths = []
    for sub in parsed_subagents:
        sub_path = write_subagent_transcript(session.session_id, sub)
        subagent_paths.append(sub_path)

    # Update DB with summary, transcript, and subagent info
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
        files_touched=", ".join(enriched_files) if enriched_files else None,
        tools_used=session.tools_used or None,
        summary=summary,
        transcript_path=transcript_path,
        subagent_transcripts=", ".join(subagent_paths) if subagent_paths else None,
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
