#!/usr/bin/env python3
"""Pi indexing entry point for the session-index Pi extension.

Usage:
    uv run hooks/pi_index.py --mode turn --session-file <path>
    uv run hooks/pi_index.py --mode exit --session-file <path>
    uv run hooks/pi_index.py --mode full --session-file <path>

This script is intentionally non-interactive and exits 0 on errors so Pi's UI is
never blocked by indexing failures.
"""

import argparse
import json
import os
import sys

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log


def main() -> None:
    parser = argparse.ArgumentParser(description="Index a Pi session")
    parser.add_argument("--mode", choices=("fast", "full", "turn", "exit", "workflow", "side-chat", "side-chat-close"), required=True)
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--transfer-source-file", help="Successful worktree transfer source; live entries arrive as JSON on stdin")
    args = parser.parse_args()

    session_file = os.path.realpath(os.path.expanduser(args.session_file))
    if not os.path.exists(session_file):
        log("pi", "pi_index", f"missing session file: {session_file}")
        return

    if args.mode in {"side-chat", "side-chat-close"}:
        from indexer import IndexOptions, IndexStage, index_source_transcript
        from side_chats import refresh_headlines
        from pi_continuations import refresh_paths

        for path in refresh_paths(session_file):
            result = index_source_transcript("pi", path, IndexOptions(frozenset({
                IndexStage.SESSION_METADATA, IndexStage.CLEAN_TRANSCRIPT,
            })))
            if not result.skipped_reason and args.mode == "side-chat-close":
                refresh_headlines(result.session_id)
        return

    if args.mode in {"turn", "exit", "workflow"}:
        from indexer import parse_session_file
        from pi_continuations import record_transfer, refresh_paths, resolve_identity
        from session_refresh import enqueue_refresh

        if args.transfer_source_file:
            if args.mode != "workflow":
                raise ValueError("Transfers require workflow mode")
            payload = json.load(sys.stdin)
            if not isinstance(payload, list):
                raise ValueError("Expected live source entries")
            record_transfer(args.transfer_source_file, session_file, source_entries=payload)
        # Read the captured file, not process env inherited from a replaced runtime.
        native_id = parse_session_file("pi", session_file).native_session_id
        session_id = resolve_identity(native_id, session_file)
        event_id = os.environ.get("SESSION_INDEX_LEAF_ID", "").strip() or args.mode
        for path in refresh_paths(session_file):
            refresh_native = native_id if path == session_file else parse_session_file("pi", path).native_session_id
            job_path = enqueue_refresh(
                "pi",
                refresh_native,
                path,
                event_id=event_id,
                force_summary=args.mode == "exit",
            )
            log(session_id, "pi_index", f"{args.mode} queued ({os.path.basename(job_path)})")
        if args.mode == "workflow":
            print(json.dumps({"session_id": session_id, "queued": True}))
        return

    from indexer import index_fast, index_full

    index_func = index_fast if args.mode == "fast" else index_full
    result = index_func("pi", session_file)
    sid = result.session_id or "pi"
    if result.skipped_reason:
        log(sid, "pi_index", f"{args.mode} skipped ({result.skipped_reason})")
        return
    log(
        sid,
        "pi_index",
        f"{args.mode} indexed ({result.user_message_count} msgs, {result.files_touched} files, {result.subagents} subagents)",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log("pi", "pi_index", f"error: {e}")
        except Exception:
            pass
    sys.exit(0)
