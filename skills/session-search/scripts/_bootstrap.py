"""Shared bootstrap for session-search skill command wrappers."""
from __future__ import annotations

import os
import sys


def repo_root() -> str:
    """Return the source repo root for symlinked skill wrapper scripts."""
    scripts_dir = os.path.dirname(os.path.realpath(__file__))
    root = os.path.dirname(os.path.dirname(os.path.dirname(scripts_dir)))

    # The wrapper directory contains inspect.py, which shadows stdlib inspect
    # while importing argparse/dataclasses through cli.py. Keep the directory
    # off sys.path before importing repo modules.
    sys.path[:] = [path for path in sys.path if os.path.realpath(path or os.curdir) != scripts_dir]
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def import_cli(*names: str):
    """Import cli symbols, converting repo breakage into a one-line diagnostic.

    Without this, a broken or mid-refactor repo surfaces as a raw traceback that
    callers misread as a problem with their query and retry against uselessly.
    """
    root = repo_root()
    try:
        import cli
        return tuple(getattr(cli, name) for name in names)
    except Exception as e:
        print(
            f"session-search installation broken: {type(e).__name__}: {e} "
            f"(repo root: {root}). The search query is not the problem; "
            "fix or reinstall the session-index repo, then retry.",
            file=sys.stderr,
        )
        raise SystemExit(3)
