#!/usr/bin/env python3
"""Print recent-session context for Pi extension system-prompt injection."""

import argparse
import os
import sys

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from logger import log
from recent_context import build_recent_context


def main() -> None:
    parser = argparse.ArgumentParser(description="Build recent session context")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--session-id", default="")
    args = parser.parse_args()

    context = build_recent_context(args.cwd)
    if context:
        print(context)
        log(args.session_id or "pi", "pi_context", "printed recent context")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log("pi", "pi_context", f"error: {e}")
        except Exception:
            pass
    sys.exit(0)
