#!/usr/bin/env python3
"""Compatibility entry point for the shared active-session refresh worker."""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, REPO_ROOT)

from logger import log
from _session_refresh_worker import run_worker


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        return
    run_worker("codex", sys.argv[1].strip())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        try:
            sid = sys.argv[1] if len(sys.argv) > 1 else "codex"
            log(sid, "refresh_worker", f"error: {error}")
        except Exception:
            pass
    raise SystemExit(0)
