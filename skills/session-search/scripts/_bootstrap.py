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
