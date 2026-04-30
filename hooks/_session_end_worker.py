#!/usr/bin/env python3
"""Detached worker — full index pass for summary + transcript.

Launched by session_end.py as a detached subprocess. If LLM fails, the DB row
keeps NULL summary and backfill can repair later.

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

    from indexer import index_full

    result = index_full("claude", jsonl_path)
    if result.skipped_reason:
        log(session_id, "worker", f"skipped ({result.skipped_reason})")
        return

    if result.subagents:
        log(session_id, "worker", f"found {result.subagents} subagent(s)")
    if result.summary_generated:
        log(session_id, "worker", "summary generated")
    else:
        log(session_id, "worker", "summary failed (LLM unavailable)")
    if result.transcript_path:
        log(session_id, "worker", "transcript written")

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
