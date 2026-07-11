#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rapidfuzz>=3.0"]
# ///
"""Focused current-session wrapper for Codex."""
from __future__ import annotations

import argparse
import os
import sys


scripts_dir = os.path.dirname(os.path.realpath(__file__))
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(scripts_dir)))
sys.path[:] = [
    path
    for path in sys.path
    if os.path.realpath(path or os.curdir) != scripts_dir
]
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from cli import cmd_current_cleaned_paths


parser = argparse.ArgumentParser(
    description="Show canonical cleaned paths for the exact active Codex conversation"
)
parser.parse_args()

cmd_current_cleaned_paths()
