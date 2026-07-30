#!/usr/bin/env python3
"""Compatibility entry point that queues a forced Claude refresh."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log
from session_refresh import enqueue_refresh


def main() -> None:
    if len(sys.argv) < 3:
        return
    session_id = sys.argv[1]
    transcript_path = sys.argv[2]
    if not os.path.exists(transcript_path):
        log(session_id, "refresh_worker", f"jsonl not found: {transcript_path}")
        return
    enqueue_refresh(
        "claude",
        session_id,
        transcript_path,
        event_id="legacy-session-end",
        force_summary=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        try:
            sid = sys.argv[1] if len(sys.argv) > 1 else "??????"
            log(sid, "refresh_worker", f"error: {error}")
        except Exception:
            pass
    raise SystemExit(0)
