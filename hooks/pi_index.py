#!/usr/bin/env python3
"""Pi indexing entry point for the session-index Pi extension.

Usage:
    uv run hooks/pi_index.py --mode fast --session-file <path>
    uv run hooks/pi_index.py --mode full --session-file <path>

This script is intentionally non-interactive and exits 0 on errors so Pi's UI is
never blocked by indexing failures.
"""

import argparse
import os
import sys

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log


def main() -> None:
    parser = argparse.ArgumentParser(description="Index a Pi session")
    parser.add_argument("--mode", choices=("fast", "full"), required=True)
    parser.add_argument("--session-file", required=True)
    args = parser.parse_args()

    session_file = os.path.realpath(os.path.expanduser(args.session_file))
    if not os.path.exists(session_file):
        log("pi", "pi_index", f"missing session file: {session_file}")
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
